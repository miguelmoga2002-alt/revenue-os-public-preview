"""ACL-aware CRM workspace backed exclusively by persisted records and Focus work."""
import json
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import or_, select
from sqlalchemy.orm import Session
from .access import readable_record, visible_record_condition
from .auth import admin, principal, record_audit
from .db import get_db
from .models import CRMRecord, CRMLink, PipelineStage, Tenant, User, WorkItem, WorkPolicy, WorkResult

router = APIRouter(prefix="/api/v1", tags=["CRM Workspace"])

class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

class StageSetup(Strict):
    stages: list[str] = Field(min_length=1, max_length=15)

class MoveDeal(Strict):
    stage: str = Field(min_length=1, max_length=80)
    expected_stage: str | None

def mode(db, user):
    p = db.get(WorkPolicy, user.tenant_id)
    return p.mode if p else "guided"

def pipeline_permission(db, user):
    setting = mode(db, user)
    if user.role not in ("admin", "rep"):
        raise HTTPException(403, "Rol sin acceso")
    if user.role == "rep" and setting == "guided":
        raise HTTPException(403, "Modo Guiado obligatorio: utiliza Focus")
    return setting

def stage_names(db, tenant_id):
    return list(db.scalars(select(PipelineStage.name).where(
        PipelineStage.tenant_id == tenant_id
    ).order_by(PipelineStage.position, PipelineStage.name)).all())

def record_data(record):
    try:
        custom = json.loads(record.attributes or "{}")
    except (ValueError, TypeError):
        custom = {}
    return {
        "id": record.id, "kind": record.kind, "name": record.name,
        "email": record.email, "phone": record.phone,
        "stage": record.stage, "amount": record.amount, "owner": record.owner,
        "source": record.source, "external_id": record.external_id,
        "updated_at": record.updated_at.isoformat(), "attributes": custom,
    }

@router.get("/pipeline/stages")
def list_stages(db: Session = Depends(get_db), user: User = Depends(principal)):
    return {"mode": mode(db, user), "stages": stage_names(db, user.tenant_id)}

@router.put("/pipeline/stages")
def configure_stages(data: StageSetup, db: Session = Depends(get_db),
                     user: User = Depends(admin)):
    names = [name.strip() for name in data.stages]
    if any(not name or len(name) > 80 for name in names):
        raise HTTPException(422, "Etapa vacía o demasiado larga")
    if len({name.casefold() for name in names}) != len(names):
        raise HTTPException(422, "Etapas repetidas")
    if db.get_bind().dialect.name == "postgresql":
        db.scalar(select(Tenant.id).where(Tenant.id == user.tenant_id).with_for_update())
    old = stage_names(db, user.tenant_id)
    removed = set(old) - set(names)
    if removed and db.scalar(select(CRMRecord.id).where(
        CRMRecord.tenant_id == user.tenant_id,
        CRMRecord.kind == "deals", CRMRecord.stage.in_(removed)
    ).limit(1)):
        raise HTTPException(409, "Hay oportunidades en etapas que quieres eliminar")
    if old == names:
        return {"stages": names, "changed": False}
    db.query(PipelineStage).filter(PipelineStage.tenant_id == user.tenant_id).delete(
        synchronize_session=False)
    db.flush()
    db.add_all([PipelineStage(tenant_id=user.tenant_id, name=n, position=i)
                for i, n in enumerate(names)])
    record_audit(db, user, "pipeline.configure", user.tenant_id,
                 {"before": old, "after": names})
    db.commit()
    return {"stages": names, "changed": True}

@router.get("/pipeline/board")
def board(db: Session = Depends(get_db), user: User = Depends(principal)):
    current = pipeline_permission(db, user)
    stages = stage_names(db, user.tenant_id)
    deals = db.scalars(select(CRMRecord).where(
        visible_record_condition(user), CRMRecord.kind == "deals"
    ).order_by(CRMRecord.updated_at.desc(), CRMRecord.id).limit(501)).all()
    lanes = [{"name": name, "deals": []} for name in stages]
    buckets = {lane["name"]: lane["deals"] for lane in lanes}
    unmapped = []
    for deal in deals[:500]:
        item = {
            "id": deal.id, "name": deal.name, "stage": deal.stage,
            "amount": deal.amount, "owner": deal.owner, "source": deal.source,
            "external_id": deal.external_id,
        }
        (buckets[deal.stage] if deal.stage in buckets else unmapped).append(item)
    return {"mode": current, "stages": stages, "lanes": lanes,
            "unmapped": unmapped, "truncated": len(deals) > 500}

@router.patch("/pipeline/deals/{deal_id}/stage")
def move_deal(deal_id: str, data: MoveDeal, db: Session = Depends(get_db),
              user: User = Depends(principal)):
    pipeline_permission(db, user)
    # Keep stage configuration and moves serialized under the same tenant lock.
    if db.get_bind().dialect.name == "postgresql":
        db.scalar(select(Tenant.id).where(Tenant.id == user.tenant_id).with_for_update())
    if data.stage not in stage_names(db, user.tenant_id):
        raise HTTPException(422, "Etapa no configurada para esta empresa")
    deal = db.scalar(select(CRMRecord).where(
        visible_record_condition(user),
        CRMRecord.id == deal_id, CRMRecord.kind == "deals"
    ).with_for_update())
    if not deal:
        raise HTTPException(404, "Oportunidad no accesible")
    # External read-only connector: do not pretend local stage moves update Pipedrive.
    try:
        imported_origin = json.loads(deal.attributes or "{}").get("origin")
    except (ValueError, TypeError, AttributeError):
        imported_origin = None
    if imported_origin in ("pipedrive_api_v2","pipedrive_oauth","hubspot_oauth","salesforce_oauth"):
        raise HTTPException(409, "Oportunidad sincronizada en modo solo lectura; modifica la etapa en Pipedrive")

    if deal.stage != data.expected_stage:
        raise HTTPException(409, "Etapa modificada; actualiza el tablero")
    if deal.stage == data.stage:
        return {"id": deal.id, "stage": deal.stage, "changed": False}
    old = deal.stage
    deal.stage = data.stage
    record_audit(db, user, "pipeline.deal.move", deal.id,
                 {"from": old, "to": data.stage})
    db.commit()
    return {"id": deal.id, "stage": deal.stage, "changed": True}

@router.get("/crm/{record_id}/360")
def account_360(record_id: str, db: Session = Depends(get_db),
                user: User = Depends(principal)):
    root = readable_record(db, user, record_id)
    if not root:
        raise HTTPException(404, "Registro no accesible")
    links = db.scalars(select(CRMLink).where(
        CRMLink.tenant_id == user.tenant_id,
        or_(CRMLink.record_id == record_id, CRMLink.related_id == record_id)
    ).limit(501)).all()
    ids = {link.related_id if link.record_id == record_id else link.record_id
           for link in links}
    ids.discard(record_id)
    related = []
    overflow = False
    if ids:
        rows = db.scalars(select(CRMRecord).where(
            visible_record_condition(user), CRMRecord.id.in_(ids)
        ).order_by(CRMRecord.kind, CRMRecord.name, CRMRecord.id).limit(101)).all()
        overflow = len(rows) > 100
        related = [record_data(row) for row in rows[:100]]
    # A rep sees only own tasks and results for the records she can read.
    visible_ids = [record_id] + [row["id"] for row in related]
    q = select(WorkItem).where(WorkItem.tenant_id == user.tenant_id,
                               WorkItem.record_id.in_(visible_ids),
                               WorkItem.state.in_(("open", "active")))
    if user.role != "admin":
        q = q.where(WorkItem.owner_id == user.id)
    due = db.scalar(q.order_by(WorkItem.due_at, WorkItem.id).limit(1))
    history = select(WorkResult, WorkItem).join(
        WorkItem, WorkItem.id == WorkResult.task_id
    ).where(
        WorkResult.tenant_id == user.tenant_id,
        WorkItem.tenant_id == user.tenant_id,
        WorkItem.record_id.in_(visible_ids)
    )
    if user.role != "admin":
        history = history.where(WorkItem.owner_id == user.id,
                                WorkResult.owner_id == user.id)
    results = db.execute(history.order_by(
        WorkResult.created_at.desc(), WorkResult.id).limit(20)).all()
    return {
        "record": record_data(root),
        "related": related,
        "related_truncated": len(links) > 500 or overflow,
        "next_commitment": ({
            "id": due.id, "title": due.title, "channel": due.channel,
            "state": due.state, "due_at": due.due_at.isoformat(),
            "basis": "work_items.due_at",
        } if due else None),
        "recent_results": [{
            "task_id": task.id, "task": task.title,
            "outcome": res.outcome, "note": res.note,
            "created_at": res.created_at.isoformat(), "source": "work_results",
        } for res, task in results],
        "data_sources": ["crm_records", "crm_links", "work_items", "work_results"],
    }


from .deal_actions import router as own_tasks_router
router.include_router(own_tasks_router, prefix='/deals')
