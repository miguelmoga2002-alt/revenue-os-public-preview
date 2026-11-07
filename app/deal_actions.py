"""Rep-owned follow-ups on an authorized deal, without changing deal ownership.

All writes are tenant-scoped, protected by record grants, and auditable.
Rep-created tasks use the existing durable creation receipt.
Reschedule idempotency uses a deterministic audit PK under the per-tenant DB lock.
"""
import hashlib
import json
from datetime import datetime, timedelta
from uuid import uuid5, NAMESPACE_URL

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from .auth import principal, utcnow, aware
from .db import get_db
from .access import readable_record
from .models import Audit, User, WorkItem, TaskCreateReceipt
from .relationships import lock_tenant_crm
from .work import TaskIn, new_task

router = APIRouter(tags=["Deal actions"])


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class OwnTask(Strict):
    idem: str = Field(min_length=8, max_length=120, pattern=r"^[A-Za-z0-9_-]+$")
    title: str = Field(min_length=2, max_length=300)
    channel: str = Field(pattern=r"^(call|email|whatsapp|meeting|research|other)$")
    due_at: datetime


class Replan(Strict):
    idem: str = Field(min_length=8, max_length=120, pattern=r"^[A-Za-z0-9_-]+$")
    expected_due_at: datetime
    due_at: datetime


def future_date(value: datetime):
    if value.tzinfo is None or value.utcoffset() is None:
        raise HTTPException(422, "Indica una fecha con zona horaria explícita")
    normalized = aware(value)
    now = utcnow()
    if normalized < now + timedelta(minutes=5) or normalized > now + timedelta(days=365):
        raise HTTPException(422, "Programa entre 5 minutos y 365 días desde ahora")
    return normalized


def own_deal(db: Session, user: User, deal_id: str):
    if user.role != "rep":
        raise HTTPException(403, "Esta operación corresponde al comercial")
    deal = readable_record(db, user, deal_id)
    if not deal or deal.kind != "deals":
        raise HTTPException(404, "Oportunidad no accesible")
    return deal


@router.get("/{deal_id}/my-tasks")
def my_tasks(deal_id: str, db: Session = Depends(get_db),
             user: User = Depends(principal)):
    own_deal(db, user, deal_id)
    rows = db.scalars(select(WorkItem).where(
        WorkItem.tenant_id == user.tenant_id,
        WorkItem.record_id == deal_id,
        WorkItem.owner_id == user.id,
        WorkItem.state.in_(("open", "active"))
    ).order_by(WorkItem.due_at, WorkItem.id).limit(101)).all()
    return {"items": [
        {"id": row.id, "title": row.title, "due_at": aware(row.due_at).isoformat(),
         "channel": row.channel, "state": row.state} for row in rows[:100]
    ], "truncated": len(rows) > 100}


@router.post("/{deal_id}/my-tasks")
def create_my_task(deal_id: str, item: OwnTask,
                   db: Session = Depends(get_db), user: User = Depends(principal)):
    own_deal(db, user, deal_id)
    if item.due_at.tzinfo is None or item.due_at.utcoffset() is None:
        raise HTTPException(422, "Indica una fecha con zona horaria explícita")
    # A previously accepted request remains replayable even if the due time
    # has since passed. The underlying constructor checks actor and payload.
    existing = db.get(TaskCreateReceipt, (user.tenant_id, item.idem))
    due = aware(item.due_at) if existing else future_date(item.due_at)
    payload = TaskIn(
        idem=item.idem, owner_id=user.id, record_id=deal_id,
        title=item.title, channel=item.channel,
        due_at=due, priority_source="manual",
    )
    # The existing task constructor has durable per-tenant receipts and a tenant
    # transaction lock. The user has been constrained to own an accessible deal.
    return new_task(item=payload, db=db, user=user)


@router.patch("/{deal_id}/my-tasks/{task_id}")
def replan_my_task(deal_id: str, task_id: str, item: Replan,
                   db: Session = Depends(get_db), user: User = Depends(principal)):
    own_deal(db, user, deal_id)
    if item.expected_due_at.tzinfo is None or item.expected_due_at.utcoffset() is None:
        raise HTTPException(422, "La fecha esperada requiere zona horaria")
    if item.due_at.tzinfo is None or item.due_at.utcoffset() is None:
        raise HTTPException(422, "La nueva fecha requiere zona horaria")
    due = aware(item.due_at)
    expected = aware(item.expected_due_at)
    lock_tenant_crm(db, user.tenant_id)
    task = db.scalar(select(WorkItem).where(
        WorkItem.id == task_id, WorkItem.tenant_id == user.tenant_id,
        WorkItem.owner_id == user.id, WorkItem.record_id == deal_id
    ).with_for_update())
    if not task:
        raise HTTPException(404, "Tarea propia no disponible")

    canonical = json.dumps({
        "task_id": task_id, "deal_id": deal_id,
        "expected_due_at": expected.isoformat(), "due_at": due.isoformat(),
    }, sort_keys=True, separators=(",", ":"))
    fingerprint = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    # Audit primary key provides an immutable, unique replay receipt without a
    # new table, serialized by lock_tenant_crm in PostgreSQL.
    receipt_id = str(uuid5(NAMESPACE_URL,
                           "revenue-os/task.replan/" + user.tenant_id + "/" +
                           user.id + "/" + item.idem))
    prior = db.get(Audit, receipt_id)
    if prior:
        try:
            detail = json.loads(prior.detail)
        except (TypeError, ValueError):
            detail = {}
        if (prior.tenant_id != user.tenant_id or prior.user_id != user.id
                or prior.action != "task.replan" or prior.target != deal_id
                or detail.get("payload_sha") != fingerprint):
            raise HTTPException(409, "Clave de operación reutilizada con otros datos")
        return {"id": task.id, "due_at": detail["to"],
                "already_recorded": True, "changed": True}
    future_date(item.due_at)
    if task.state != "open":
        raise HTTPException(409, "Solo se pueden reprogramar tareas pendientes")
    # A rep must resolve an already due action through Focus, rather than
    # silently postponing a top-ranked task to bypass guided execution.
    if aware(task.due_at) <= utcnow():
        raise HTTPException(409, "La tarea ya vence o está vencida: registra el resultado en Focus")
    if aware(task.due_at) != expected:
        raise HTTPException(409, "La tarea cambió; actualiza la oportunidad")
    if expected == due:
        return {"id": task.id, "due_at": expected.isoformat(),
                "already_recorded": False, "changed": False}
    task.due_at = due
    db.add(Audit(
        id=receipt_id, tenant_id=user.tenant_id, user_id=user.id,
        action="task.replan", target=deal_id,
        detail=json.dumps({"task_id": task.id, "title": task.title,
                           "from": expected.isoformat(), "to": due.isoformat(),
                           "payload_sha": fingerprint}, ensure_ascii=False),
    ))
    db.commit()
    return {"id": task.id, "due_at": due.isoformat(),
            "already_recorded": False, "changed": True}
