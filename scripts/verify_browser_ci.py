"""Read-only SQL assertions after Chromium E2E on an isolated CI database."""
import os

from sqlalchemy import select, func, text

from app.db import SessionLocal, engine
from app.models import Audit, CRMRecord, WorkItem, WorkResult, DealNote
from app.tenant_context import set_tenant_context

TENANT = "ci-browser-v11-a"


def main():
    if (os.getenv("GITHUB_ACTIONS") != "true"
            or os.getenv("REVENUE_OS_E2E_SEED") != "synthetic-ci-only"):
        raise SystemExit("Refusing to verify non-CI database")
    with engine.connect() as conn:
        name, role = conn.execute(text("SELECT current_database(), current_user")).one()
        assert (name, role) == ("revenue_os", "revenue_app")

    with SessionLocal() as db:
        set_tenant_context(db, TENANT)
        actions = set(db.scalars(select(Audit.action).where(
            Audit.tenant_id == TENANT)).all())
        must_have = {
            "login", "migration.commit", "migration.reconcile",
            "pipeline.configure", "pipeline.deal.move",
            "task.create", "task.replan", "task.claim", "task.complete", "deal.note.create",
        }
        assert must_have <= actions, must_have - actions
        deal = db.scalar(select(CRMRecord).where(
            CRMRecord.tenant_id == TENANT,
            CRMRecord.kind == "deals",
            CRMRecord.external_id == "D1",
        ))
        assert deal and deal.stage == "Proposal"
        note = db.scalar(select(WorkResult.note).where(
            WorkResult.tenant_id == TENANT,
            WorkResult.note == "CI: lead atendido desde Chromium",
        ))
        assert note == "CI: lead atendido desde Chromium"
        assigned_count = db.scalar(select(func.count()).select_from(WorkItem).where(
            WorkItem.tenant_id == TENANT,
            WorkItem.title == "E2E: Llamar al lead QA",
        ))
        assert assigned_count == 1, assigned_count
        self_action = db.scalars(select(WorkItem).where(
            WorkItem.tenant_id == TENANT,
            WorkItem.title == "E2E: Preparar propuesta propia",
        )).all()
        assert len(self_action) == 1, "Rep follow-up must be persisted exactly once"
        changes = db.scalars(select(Audit).where(
            Audit.tenant_id == TENANT,
            Audit.action == "task.replan",
            Audit.target == deal.id,
        )).all()
        assert len(changes) == 1, "Replanning must be auditable and idempotent"
        followup = db.scalar(select(WorkItem).where(
            WorkItem.tenant_id == TENANT,
            WorkItem.title == "Enviar propuesta QA",
            WorkItem.state == "open",
        ))
        assert followup and followup.record_id
        result_count = db.scalar(select(func.count()).select_from(WorkResult).where(
            WorkResult.tenant_id == TENANT))
        assert result_count == 1, result_count
        notes = db.scalars(select(DealNote).where(
            DealNote.tenant_id == TENANT, DealNote.deal_id == deal.id,
        )).all()
        assert len(notes) == 2, f"Expected two notes (CRM and Focus), got {len(notes)}"
        assert {n.body for n in notes} == {
            "CI: nota inicial del deal, sin comunicación externa",
            "CI: nota comercial guardada desde Focus",
        }
        print("PASS: PostgreSQL stores deal notes, move, follow-up, one result and audit trail.")


if __name__ == "__main__":
    main()
