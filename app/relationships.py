"""CRM external-ID relationship reconciliation within one tenant and source.

Reconciliation is explicit after CSV import; it never guesses across providers.
"""
import hashlib
import json
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session
from .models import CRMRecord, CRMLink, Tenant

RELATIONS = (("company_external_id", "companies", "company"), ("contact_external_id", "contacts", "contact"), ("deal_external_id", "deals", "deal"))


def lock_tenant_crm(db: Session, tenant_id: str) -> None:
    """Serialize CRM imports, rollback, and reconciliations per tenant in PostgreSQL.

    All writers in this codebase must acquire this lock before changing CRM
    records or their links. SQLite remains a lab-only fallback.
    """
    if db.get_bind().dialect.name == "postgresql":
        if not db.scalar(select(Tenant.id).where(Tenant.id == tenant_id).with_for_update()):
            raise HTTPException(404, "Empresa no encontrada")


def reconcile(db: Session, tenant_id: str, source: str, persist: bool = False,
              expected_snapshot: str | None = None) -> dict:
    records = db.scalars(select(CRMRecord).where(
        CRMRecord.tenant_id == tenant_id,
        CRMRecord.source == source,
        CRMRecord.kind.in_(("contacts", "deals", "activities"))
    )).all()
    related = db.scalars(select(CRMRecord).where(
        CRMRecord.tenant_id == tenant_id,
        CRMRecord.source == source,
        CRMRecord.kind.in_(("companies", "contacts", "deals"))
    )).all()
    lookup = {(r.kind, r.external_id): r for r in related}
    links = db.scalars(select(CRMLink).where(CRMLink.tenant_id == tenant_id)).all()
    current = {(link.record_id, link.relation): link for link in links}

    # Fingerprint every source-side reference and existing link, with no 100-row
    # preview truncation. It is checked *inside* the locked write transaction.
    all_rows = {row.id: row for row in records + related}
    sources = sorted(
        (row.id, row.kind, row.external_id, row.company_external_id,
         row.contact_external_id, row.deal_external_id)
        for row in all_rows.values()
    )
    source_ids = set(all_rows)
    existing_links = sorted(
        (row.record_id, row.relation, row.related_id)
        for row in links if row.record_id in source_ids
    )
    snapshot_data = json.dumps([tenant_id, source, sources, existing_links],
                               separators=(",", ":"), ensure_ascii=False)
    snapshot = hashlib.sha256(snapshot_data.encode("utf-8")).hexdigest()
    if persist and expected_snapshot != snapshot:
        raise HTTPException(409, "Las relaciones han cambiado; revisa de nuevo antes de guardar")

    resolved = 0
    created = 0
    changed = 0
    removed = 0
    missing = []
    for record in records:
        for field, target_kind, relation in RELATIONS:
            if relation == "deal" and record.kind != "activities":
                continue
            key = (record.id, relation)
            existing = current.get(key)
            external_id = getattr(record, field)
            target = lookup.get((target_kind, external_id)) if external_id else None
            # Existing relations are invalidated when source IDs change or
            # source references disappear; no cross-source lookups allowed.
            if not external_id or target is None:
                if existing and persist:
                    db.delete(existing)
                    removed += 1
                if external_id and target is None:
                    missing.append({
                        "record_id": record.id, "kind": record.kind,
                        "external_id": record.external_id,
                        "field": field, "missing_reference": external_id
                    })
                continue
            resolved += 1
            if persist:
                if existing is None:
                    db.add(CRMLink(tenant_id=tenant_id, record_id=record.id,
                                   relation=relation, related_id=target.id))
                    created += 1
                elif existing.related_id != target.id:
                    existing.related_id = target.id
                    changed += 1

    return {
        "source": source,
        "snapshot_sha256": snapshot,
        "resolved_references": resolved,
        "unresolved_count": len(missing),
        "unresolved": missing[:100],
        "links_created": created,
        "links_updated": changed,
        "links_removed": removed,
        "persisted": persist,
    }
