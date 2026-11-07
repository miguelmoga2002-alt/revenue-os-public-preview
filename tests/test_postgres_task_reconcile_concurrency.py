"""PostgreSQL two-connection races for task creation and CRM reconciliation.

Run only against a dedicated *_test DB as revenue_app; each test deletes only
its own synthetic tenant and records after joining all threads.
"""
import os
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from threading import Barrier
from uuid import uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session

from app.auth import hash_password
from app.models import Tenant, User, CRMRecord
from app.tenant_context import set_tenant_context
from app.main import persist_relationships, ReconcileApproval
from app.relationships import reconcile
from app.work import new_task, TaskIn

URL = os.getenv("REVENUE_OS_TEST_POSTGRES_URL", "")
PASS = os.getenv("REVENUE_OS_TEST_APP_PASSWORD", "")


def engines():
    url = make_url(URL)
    if not url.drivername.startswith("postgresql") or not url.database.endswith("_test"):
        pytest.fail("Requires isolated PostgreSQL *_test database")
    owner = create_engine(URL, pool_pre_ping=True)
    app = create_engine(url.set(username="revenue_app", password=PASS),
                        pool_size=4, pool_pre_ping=True)
    return owner, app


def run_parallel(engine, tenant, fn):
    barrier = Barrier(2)
    def call(index):
        with Session(engine, expire_on_commit=False) as db:
            set_tenant_context(db, tenant)
            barrier.wait(timeout=15)
            try:
                return ("ok", fn(index, db))
            except HTTPException as error:
                db.rollback()
                return ("error", error.status_code)
    with ThreadPoolExecutor(max_workers=2) as pool:
        return list(pool.map(call, range(2)))


def clean(owner, tenant):
    with owner.begin() as conn:
        for table in ("task_create_receipts", "audit_events", "record_grants",
                      "work_results", "work_leases", "work_items", "crm_links",
                      "crm_records", "work_policies", "pipeline_stages"):
            conn.execute(text(f"DELETE FROM {table} WHERE tenant_id=:t"), {"t": tenant})
        conn.execute(text("DELETE FROM users WHERE tenant_id=:t"), {"t": tenant})
        conn.execute(text("DELETE FROM tenants WHERE id=:t"), {"t": tenant})


@pytest.mark.skipif(not URL or not PASS, reason="Restricted PostgreSQL test DB required")
def test_two_managers_submit_same_task_key_once():
    owner, app = engines()
    suffix = uuid4().hex[:12]
    tenant, admin, rep, record = [f"ci-idem-{suffix}-{x}" for x in ("t", "a", "r", "c")]
    actor = User(id=admin, tenant_id=tenant, role="admin",
                 username="idem-admin-"+suffix,
                 password_hash=hash_password("synthetic-test-only-password"))
    try:
        with Session(owner, expire_on_commit=False) as db:
            db.add(Tenant(id=tenant, name="Task idempotency test"))
            db.flush()
            db.add(actor)
            db.add(User(id=rep, tenant_id=tenant, role="rep",
                        username="idem-rep-"+suffix,
                        password_hash=hash_password("synthetic-test-only-password")))
            db.add(CRMRecord(id=record, tenant_id=tenant, kind="contacts", source="ci",
                             external_id="P1", name="Synthetic lead", attributes="{}"))
            db.commit()
        payload = TaskIn(
            idem="same-request-"+suffix, owner_id=rep, record_id=record,
            title="Call lead", channel="call",
            due_at=datetime(2026, 1, 1, 9, tzinfo=timezone.utc),
        )
        responses = run_parallel(app, tenant, lambda index, db:
                                 new_task(item=payload, db=db, user=actor))
        assert all(kind == "ok" for kind, _ in responses), responses
        assert sorted(x["already_recorded"] for _, x in responses) == [False, True]
        assert responses[0][1]["id"] == responses[1][1]["id"]
        with Session(owner) as db:
            for table, expected in (("work_items", 1), ("task_create_receipts", 1),
                                    ("record_grants", 1)):
                assert db.scalar(text(f"SELECT count(*) FROM {table} WHERE tenant_id=:t"),
                                 {"t": tenant}) == expected
            assert db.scalar(text("SELECT COUNT(*) FROM audit_events WHERE tenant_id=:t "
                                  "AND action='task.create'"), {"t": tenant}) == 1
        with Session(app) as db:
            set_tenant_context(db, tenant)
            with pytest.raises(HTTPException) as different:
                new_task(item=payload.model_copy(update={"title": "Another call"}),
                         db=db, user=actor)
            assert different.value.status_code == 409
    finally:
        clean(owner, tenant)
        app.dispose()
        owner.dispose()


@pytest.mark.skipif(not URL or not PASS, reason="Restricted PostgreSQL test DB required")
def test_two_reconciliations_same_preview_one_commit_one_409():
    owner, app = engines()
    suffix = uuid4().hex[:12]
    tenant, admin, company, lead = [f"ci-rel-{suffix}-{x}" for x in ("t", "a", "c", "l")]
    actor = User(id=admin, tenant_id=tenant, role="admin",
                 username="rel-admin-"+suffix,
                 password_hash=hash_password("synthetic-test-only-password"))
    try:
        with Session(owner, expire_on_commit=False) as db:
            db.add(Tenant(id=tenant, name="Reconcile race test"))
            db.flush()
            db.add(actor)
            db.add(CRMRecord(id=company, tenant_id=tenant, kind="companies",
                             source="pipedrive", external_id="O1", name="Acme",
                             attributes="{}"))
            db.add(CRMRecord(id=lead, tenant_id=tenant, kind="contacts",
                             source="pipedrive", external_id="P1", name="Lead",
                             company_external_id="O1", attributes="{}"))
            db.commit()
        with Session(owner) as db:
            preview = reconcile(db, tenant, "pipedrive")
        data = ReconcileApproval(expected_snapshot=preview["snapshot_sha256"])
        responses = run_parallel(app, tenant, lambda index, db:
            persist_relationships(data=data, source="pipedrive", db=db, user=actor))
        assert sum(kind == "ok" for kind, _ in responses) == 1, responses
        assert [r for kind, r in responses if kind == "error"] == [409], responses
        with Session(owner) as db:
            assert db.scalar(text("SELECT COUNT(*) FROM crm_links WHERE tenant_id=:t"),
                             {"t": tenant}) == 1
            assert db.scalar(text("SELECT COUNT(*) FROM audit_events WHERE tenant_id=:t "
                                  "AND action='migration.reconcile'"), {"t": tenant}) == 1
    finally:
        clean(owner, tenant)
        app.dispose()
        owner.dispose()
