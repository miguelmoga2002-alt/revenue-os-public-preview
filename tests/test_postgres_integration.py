"""PostgreSQL integration test. Requires a DEDICATED, migrated test database.

Set REVENUE_OS_TEST_POSTGRES_URL to a PostgreSQL database whose NAME ends in
_test. This test never truncates or drops data; all fixtures roll back.
"""
import json
import os
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, inspect, select
from sqlalchemy.orm import Session, sessionmaker

from app.auth import hash_password
from app.db import get_db
from app.main import app
from app.models import CRMRecord, Tenant, User, WorkItem, WorkPolicy

TEST_URL = os.environ.get("REVENUE_OS_TEST_POSTGRES_URL", "")

@pytest.mark.skipif(not TEST_URL, reason="Dedicated PostgreSQL test URL not configured")
def test_postgresql_migrations_and_tenant_isolation():
    from sqlalchemy.engine import make_url
    url = make_url(TEST_URL)
    if not url.drivername.startswith("postgresql") or not url.database or not url.database.endswith("_test"):
        pytest.fail("PostgreSQL tests require a dedicated database named *_test")

    engine = create_engine(TEST_URL, pool_pre_ping=True)
    required = {"tenants", "users", "sessions", "crm_records", "work_items", "work_leases",
                "work_results", "work_policies", "import_batches", "audit_events"}
    assert required.issubset(set(inspect(engine).get_table_names())), "Run alembic upgrade head first"

    # Join API-request transactions into savepoints. The outer transaction is
    # always rolled back, even if the endpoint itself calls db.commit().
    conn = engine.connect()
    outer = conn.begin()
    SessionFactory = sessionmaker(bind=conn, expire_on_commit=False,
                                  join_transaction_mode="create_savepoint")
    suffix = uuid4().hex[:12]
    ta, tb = f"pg-a-{suffix}", f"pg-b-{suffix}"
    ua, ub = f"admin-a-{suffix}", f"admin-b-{suffix}"
    password = "postgres-test-password-1234"
    try:
        with SessionFactory.begin() as db:
            db.add_all([Tenant(id=ta, name="PG Test A"), Tenant(id=tb, name="PG Test B")])
            db.add_all([
                User(id=ua, tenant_id=ta, username=ua, role="admin", password_hash=hash_password(password)),
                User(id=ub, tenant_id=tb, username=ub, role="admin", password_hash=hash_password(password)),
            ])
        # Requests run as a restricted PostgreSQL role, not the superuser.
        # SQLAlchemy savepoint sessions share this transaction-local role.
        conn.exec_driver_sql("SET LOCAL ROLE revenue_app")
        assert conn.exec_driver_sql("SELECT current_user").scalar() == "revenue_app"
        def override_db():
            with SessionFactory() as db:
                yield db
        app.dependency_overrides[get_db] = override_db
        with TestClient(app) as client:
            def login(username):
                r = client.post("/api/v1/auth/login",
                                json={"username": username, "password": password})
                assert r.status_code == 200, r.text
                return {"Authorization": "Bearer " + r.json()["access_token"]}
            ha, hb = login(ua), login(ub)
            data = b"ID,Name,Email\np-100,Example,example@example.invalid\n"
            preview = client.post("/api/v1/migrations/preview", headers=ha,
                                  data={"kind": "contacts"}, files={"file": ("a.csv", data)})
            assert preview.status_code == 200, preview.text
            spec = {"kind": "contacts", "source": "pipedrive",
                    "digest": preview.json()["digest"],
                    "columns": {"external_id": "ID", "name": "Name", "email": "Email"}}
            created = client.post("/api/v1/migrations/commit", headers=ha,
                                  data={"spec": json.dumps(spec)}, files={"file": ("a.csv", data)})
            assert created.status_code == 200, created.text
            assert created.json()["created"] == 1
            assert len(client.get("/api/v1/records", headers=ha).json()) == 1
            assert client.get("/api/v1/records", headers=hb).json() == []
            assert client.get("/api/v1/migrations/summary", headers=hb).json()["contacts"] == 0
            again = client.post("/api/v1/migrations/commit", headers=ha,
                                data={"spec": json.dumps(spec)}, files={"file": ("a.csv", data)})
            assert again.status_code == 200 and again.json()["skipped"] == 1
            # Explicit same external ID is allowed in a different tenant.
            other = client.post("/api/v1/migrations/commit", headers=hb,
                                data={"spec": json.dumps(spec)}, files={"file": ("b.csv", data)})
            assert other.status_code == 200 and other.json()["created"] == 1
            assert len(client.get("/api/v1/records", headers=hb).json()) == 1
            assert len(client.get("/api/v1/records", headers=ha).json()) == 1

            # Regression: batch_mutations holds an FK to CRM records. Rollback
            # must drop those mutation rows before deleting created records.
            demo = b"ID,Name\nRB-100,Disposable PostgreSQL QA\n"
            demo_preview = client.post("/api/v1/migrations/preview", headers=ha,
                data={"kind": "companies"}, files={"file": ("rollback.csv", demo)})
            assert demo_preview.status_code == 200, demo_preview.text
            demo_spec = {"kind": "companies", "source": "revenueos_test",
                "digest": demo_preview.json()["digest"],
                "columns": {"external_id": "ID", "name": "Name"}}
            demo_batch = client.post("/api/v1/migrations/commit", headers=ha,
                data={"spec": json.dumps(demo_spec)}, files={"file": ("rollback.csv", demo)})
            assert demo_batch.status_code == 200, demo_batch.text
            demo_id = demo_batch.json()["batch_id"]
            reversed_batch = client.post(f"/api/v1/migrations/{demo_id}/rollback", headers=ha)
            assert reversed_batch.status_code == 200, reversed_batch.text
            assert reversed_batch.json()["mutations"] == 1
            assert client.get("/api/v1/records?kind=companies", headers=ha).json() == []
            assert any(b["id"] == demo_id and b["status"] == "rolled_back"
                for b in client.get("/api/v1/migrations/batches", headers=ha).json())

            # Deal Workspace: real PostgreSQL migration, restricted runtime role
            # and direct RLS proof for persisted user-authored notes.
            workspace_csv = b"ID,Name,Stage\nDW-100,Workspace Demo,Qualified\n"
            workspace_preview = client.post("/api/v1/migrations/preview",headers=ha,
                data={"kind":"deals"},files={"file":("workspace.csv",workspace_csv)})
            assert workspace_preview.status_code == 200,workspace_preview.text
            workspace_spec={"kind":"deals","source":"revenueos_test",
                "digest":workspace_preview.json()["digest"],
                "columns":{"external_id":"ID","name":"Name","stage":"Stage"}}
            imported_deal=client.post("/api/v1/migrations/commit",headers=ha,
                data={"spec":json.dumps(workspace_spec)},files={"file":("workspace.csv",workspace_csv)})
            assert imported_deal.status_code == 200,imported_deal.text
            workspace_id=next(x["id"] for x in client.get("/api/v1/records?kind=deals",headers=ha).json()
                              if x["external_id"]=="DW-100")
            endpoint=f"/api/v1/deals/{workspace_id}/notes"
            note_input={"text":"CI postgres deal note, unverified manual","idem":"postgres-workspace-note-123"}
            note_response=client.post(endpoint,headers=ha,json=note_input)
            assert note_response.status_code == 200,note_response.text
            assert client.post(endpoint,headers=ha,json=note_input).json()["already_recorded"]
            assert client.post(endpoint,headers=hb,json=note_input).status_code == 404
            assert client.get(f"/api/v1/deals/{workspace_id}/timeline",headers=hb).status_code == 404
            assert client.post("/api/v1/migrations/"+imported_deal.json()["batch_id"]+
                "/rollback",headers=ha).status_code == 409
            assert client.get(f"/api/v1/deals/{workspace_id}/timeline",headers=ha).status_code == 200
            # The connection has SET LOCAL ROLE revenue_app. RLS denies cross-tenant
            # direct SQL, not just API calls.
            conn.exec_driver_sql("SELECT set_config('revenue_os.tenant_id', %s, true)",(ta,))
            assert conn.exec_driver_sql("SELECT COUNT(*) FROM deal_notes WHERE deal_id=%s",
                                        (workspace_id,)).scalar() == 1
            conn.exec_driver_sql("SELECT set_config('revenue_os.tenant_id', %s, true)",(tb,))
            assert conn.exec_driver_sql("SELECT COUNT(*) FROM deal_notes").scalar() == 0
            conn.exec_driver_sql("SELECT set_config('revenue_os.tenant_id', %s, true)",(ta,))

            # PostgreSQL must enforce the same tenant-scoped authorization
            # paths as the SQLite tests (without claiming database RLS).
            rep_name = f"rep-a-{suffix}"
            rep_response = client.post("/api/v1/admin/reps", headers=ha,
                                       json={"username": rep_name, "password": password})
            assert rep_response.status_code == 200, rep_response.text
            rep_id = rep_response.json()["id"]
            rep_token = login(rep_name)
            foreign_rec = client.get("/api/v1/records", headers=hb).json()[0]["id"]
            own_rec = client.get("/api/v1/records", headers=ha).json()[0]["id"]
            # Account 360 and pipeline stages must respect the restricted RLS role.
            stage = client.put("/api/v1/pipeline/stages", headers=ha,
                               json={"stages": ["Qualified", "Proposal"]})
            assert stage.status_code == 200, stage.text
            assert client.get("/api/v1/pipeline/stages", headers=hb).json()["stages"] == []
            assert client.get("/api/v1/pipeline/board", headers=ha).status_code == 200
            assert client.get(f"/api/v1/crm/{own_rec}/360", headers=ha).status_code == 200
            assert client.get(f"/api/v1/crm/{own_rec}/360", headers=hb).status_code == 404
            due = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
            foreign_task = client.post("/api/v1/work/tasks", headers=hb,
                                       json={"idem":"pg-cross-tenant-task-123", "owner_id": rep_id, "record_id": foreign_rec,
                                             "title": "Unauthorized cross-tenant task",
                                             "channel": "call", "due_at": due})
            assert foreign_task.status_code == 422, foreign_task.text
            valid_task = client.post("/api/v1/work/tasks", headers=ha,
                                     json={"idem":"pg-own-tenant-task-123", "owner_id": rep_id, "record_id": own_rec,
                                           "title": "Follow up own contact",
                                           "channel": "call", "due_at": due})
            assert valid_task.status_code == 200, valid_task.text
            task_id = valid_task.json()["id"]
            assert client.get("/api/v1/work/queue", headers=rep_token).status_code == 200
            # Neither another tenant nor a non-admin can release this task.
            assert client.post("/api/v1/work/release/" + rep_id,
                               headers=hb).status_code == 404
            assert client.post("/api/v1/work/claim/" + task_id,
                               headers=rep_token).status_code == 200
            assert client.post("/api/v1/work/claim/" + task_id,
                               headers=hb).status_code == 403
            assert client.post("/api/v1/work/release/" + rep_id,
                               headers=hb).status_code == 404
            assert client.post("/api/v1/work/release/" + rep_id,
                               headers=ha).status_code == 200
            # B must not reverse A's migration batch.
            assert client.post("/api/v1/migrations/" + created.json()["batch_id"] +
                               "/rollback", headers=hb).status_code == 404

            # Session revocation must invalidate the bearer token immediately.
            assert client.delete("/api/v1/auth/session", headers=rep_token).status_code == 200
            assert client.get("/api/v1/me", headers=rep_token).status_code == 401
        # This is app-level tenant isolation, NOT PostgreSQL RLS.
    finally:
        app.dependency_overrides.pop(get_db, None)
        outer.rollback()
        conn.close()
        engine.dispose()
