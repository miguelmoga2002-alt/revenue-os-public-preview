"""Two PostgreSQL app-role transactions must not overwrite a deal stage.

Same expected_stage, different targets: one successful move, one HTTP 409.
Uses tenant-scoped RLS runtime sessions and an isolated *_test database.
"""
import os
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from uuid import uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session

from app.auth import hash_password
from app.crm_workspace import MoveDeal, move_deal
from app.models import CRMRecord, PipelineStage, Tenant, User
from app.tenant_context import set_tenant_context

URL = os.getenv("REVENUE_OS_TEST_POSTGRES_URL", "")
PASSWORD = os.getenv("REVENUE_OS_TEST_APP_PASSWORD", "")


@pytest.mark.skipif(not URL or not PASSWORD, reason="Restricted PostgreSQL test DB required")
def test_parallel_deal_moves_have_one_winner_and_one_conflict():
    parsed = make_url(URL)
    if not parsed.drivername.startswith("postgresql") or not parsed.database.endswith("_test"):
        pytest.fail("Requires isolated PostgreSQL *_test database")
    owner_engine = create_engine(URL, pool_pre_ping=True)
    runtime_engine = create_engine(
        parsed.set(username="revenue_app", password=PASSWORD),
        pool_size=4, pool_pre_ping=True,
    )
    suffix = uuid4().hex[:12]
    tenant, actor_id, deal_id = ("tenant-"+suffix, "admin-"+suffix, "deal-"+suffix)
    actor = User(id=actor_id, tenant_id=tenant, role="admin",
                 username="stage-admin-"+suffix,
                 password_hash=hash_password("synthetic-only-test-password"))
    try:
        with Session(owner_engine, expire_on_commit=False) as db:
            db.add(Tenant(id=tenant, name="Concurrency test"))
            db.flush()
            db.add(actor)
            db.add(CRMRecord(
                id=deal_id, tenant_id=tenant, kind="deals",
                source="ci", external_id="D1", name="Concurrent opportunity",
                stage="Qualified", attributes="{}",
            ))
            db.add_all([
                PipelineStage(tenant_id=tenant, name=stage, position=i)
                for i, stage in enumerate(("Qualified", "Proposal", "Won"))
            ])
            db.commit()

        barrier = Barrier(2)
        def run(target):
            with Session(runtime_engine, expire_on_commit=False) as db:
                set_tenant_context(db, tenant)
                barrier.wait(timeout=15)
                try:
                    result = move_deal(
                        deal_id=deal_id,
                        data=MoveDeal(stage=target, expected_stage="Qualified"),
                        db=db, user=actor,
                    )
                    return ("ok", result["stage"])
                except HTTPException as exc:
                    db.rollback()
                    return ("error", exc.status_code)

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(run, ("Proposal", "Won")))
        assert sum(status == "ok" for status, _ in results) == 1, results
        assert sorted(value for status, value in results if status == "error") == [409]
        with Session(owner_engine) as db:
            stored = db.scalar(text(
                "SELECT stage FROM crm_records WHERE id=:d AND tenant_id=:t"
            ), {"d": deal_id, "t": tenant})
            assert stored in {"Proposal", "Won"}
            assert db.scalar(text(
                "SELECT COUNT(*) FROM audit_events WHERE tenant_id=:t "
                "AND action='pipeline.deal.move'"
            ), {"t": tenant}) == 1
    finally:
        with owner_engine.begin() as conn:
            for table in ("audit_events", "crm_links", "record_grants",
                          "crm_records", "pipeline_stages", "work_policies"):
                conn.execute(text(f"DELETE FROM {table} WHERE tenant_id=:t"), {"t": tenant})
            conn.execute(text("DELETE FROM users WHERE id=:u"), {"u": actor_id})
            conn.execute(text("DELETE FROM tenants WHERE id=:t"), {"t": tenant})
        runtime_engine.dispose()
        owner_engine.dispose()
