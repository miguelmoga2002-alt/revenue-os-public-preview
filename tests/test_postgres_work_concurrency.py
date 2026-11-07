"""Two separate PostgreSQL connections race to claim and complete one Focus item.

Only runs against a dedicated *_test database. Uses a non-owner runtime role.
Test entities are removed explicitly after all threads have exited.
"""
import os
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from threading import Barrier
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session

from app.auth import hash_password
from app.models import Audit, Tenant, User, WorkItem, WorkLease, WorkResult
from app.tenant_context import set_tenant_context
from app.work import ResultIn, claim, complete

DB_URL = os.environ.get("REVENUE_OS_TEST_POSTGRES_URL", "")
APP_PASSWORD = os.environ.get("REVENUE_OS_TEST_APP_PASSWORD", "")


@pytest.mark.skipif(not DB_URL or not APP_PASSWORD, reason="PostgreSQL test and restricted role required")
def test_concurrent_claim_and_completion_idempotency():
    url = make_url(DB_URL)
    if not url.drivername.startswith("postgresql") or not url.database.endswith("_test"):
        pytest.fail("Concurrency test requires dedicated PostgreSQL *_test database")
    admin_engine = create_engine(DB_URL, pool_pre_ping=True)
    runtime_url = url.set(username="revenue_app", password=APP_PASSWORD)
    app_engine = create_engine(runtime_url, pool_size=4, max_overflow=0, pool_pre_ping=True)
    suffix = uuid4().hex[:16]
    tenant_id, owner_id, task_id = ("t-" + suffix, "u-" + suffix, "w-" + suffix)
    actor = User(id=owner_id, tenant_id=tenant_id, role="rep",
                 username="concurrent-" + suffix,
                 password_hash=hash_password("test-only-not-a-production-password"))
    try:
        with Session(admin_engine, expire_on_commit=False) as db:
            db.add(Tenant(id=tenant_id, name="Concurrency test"))
            db.flush()
            db.add(actor)
            db.flush()
            db.add(WorkItem(id=task_id, tenant_id=tenant_id, owner_id=owner_id,
                            title="Call lead", channel="call", state="open",
                            due_at=datetime.now(timezone.utc)-timedelta(minutes=30)))
            db.commit()

        def parallel_calls(operation):
            barrier = Barrier(2)
            def call_one(_):
                with Session(app_engine, expire_on_commit=False) as db:
                    set_tenant_context(db, tenant_id)
                    barrier.wait(timeout=10)
                    return operation(db)
            with ThreadPoolExecutor(max_workers=2) as pool:
                return list(pool.map(call_one, range(2)))

        claims = parallel_calls(lambda db: claim(task_id=task_id, db=db, user=actor))
        assert all(r["state"] == "active" and r["id"] == task_id for r in claims)
        with Session(admin_engine) as db:
            assert db.scalar(text(
                "SELECT count(*) FROM work_leases WHERE tenant_id=:t AND owner_id=:o"
            ), {"t": tenant_id, "o": owner_id}) == 1
            assert db.scalar(text(
                "SELECT count(*) FROM audit_events WHERE tenant_id=:t AND action='task.claim'"
            ), {"t": tenant_id}) == 1

        idem = "parallel-complete-" + suffix
        results = parallel_calls(lambda db: complete(
            data=ResultIn(task_id=task_id, idem=idem, outcome="done"),
            db=db, user=actor
        ))
        assert sorted(r["already_recorded"] for r in results) == [False, True]
        assert results[0]["result_id"] == results[1]["result_id"]
        with Session(admin_engine) as db:
            assert db.scalar(text(
                "SELECT count(*) FROM work_results WHERE tenant_id=:t"
            ), {"t": tenant_id}) == 1
            assert db.scalar(text(
                "SELECT count(*) FROM work_leases WHERE tenant_id=:t"
            ), {"t": tenant_id}) == 0
            assert db.scalar(text(
                "SELECT state FROM work_items WHERE id=:w"
            ), {"w": task_id}) == "done"
    finally:
        # No test-owned objects may remain in the shared CI database.
        with admin_engine.begin() as conn:
            for table in ("audit_events", "work_results", "work_leases",
                          "work_policies", "work_items"):
                conn.execute(text(f"DELETE FROM {table} WHERE tenant_id=:t"), {"t": tenant_id})
            conn.execute(text("DELETE FROM users WHERE id=:o"), {"o": owner_id})
            conn.execute(text("DELETE FROM tenants WHERE id=:t"), {"t": tenant_id})
        app_engine.dispose()
        admin_engine.dispose()
