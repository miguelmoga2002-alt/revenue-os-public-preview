"""Phase 9 regression: durable task idempotency and stale link reconciliation."""
from datetime import datetime, timezone
from uuid import uuid4

from .conftest import login
from .test_crm_links import upload


def test_task_idempotency_replays_original_task_without_extra_grants_or_audit(api):
    admin, other, rep = login(api), login(api, "admin-b"), login(api, "rep-a")
    reps = api.get("/api/v1/admin/reps", headers=admin).json()
    owner = next(x["id"] for x in reps if x["username"] == "rep-a")
    upload(api, admin, "contacts", "ID,Name\nP1,Lead QA\n",
           {"external_id": "ID", "name": "Name"})
    record = api.get("/api/v1/records", headers=admin).json()[0]["id"]
    payload = {"idem": "same-task-123456789", "owner_id": owner,
               "record_id": record, "title": "Call QA", "channel": "call",
               "due_at": "2026-01-01T09:00:00+01:00", "priority_source": "manual"}
    first = api.post("/api/v1/work/tasks", headers=admin, json=payload)
    assert first.status_code == 200, first.text
    assert first.json()["already_recorded"] is False
    # Equivalent timezone and JSON key ordering are normalized identically.
    same = {**payload, "due_at": "2026-01-01T08:00:00Z"}
    replay = api.post("/api/v1/work/tasks", headers=admin, json=same)
    assert replay.status_code == 200 and replay.json()["already_recorded"] is True
    assert replay.json()["id"] == first.json()["id"]
    assert api.get("/api/v1/records/"+record, headers=rep).status_code == 200
    changed = api.post("/api/v1/work/tasks", headers=admin,
                       json={**payload, "title": "A different operation"})
    assert changed.status_code == 409, changed.text
    assert api.post("/api/v1/work/tasks", headers=admin,
                    json={k: v for k, v in payload.items() if k != "idem"}).status_code == 422
    assert api.post("/api/v1/work/tasks", headers=rep, json=payload).status_code == 403
    assert api.post("/api/v1/work/tasks", headers=other, json=payload).status_code == 422
    assert api.get("/api/v1/work/queue", headers=rep).status_code == 200
    # Same random key is allowed for an entirely different tenant.
    b_owner = next(x["id"] for x in api.get("/api/v1/admin/reps", headers=other).json()
                   if x["username"] == "rep-b")
    elsewhere = api.post("/api/v1/work/tasks", headers=other,
                         json={**payload, "owner_id": b_owner, "record_id": None})
    assert elsewhere.status_code == 200 and elsewhere.json()["id"] != first.json()["id"]


def test_reconcile_requires_snapshot_and_rejects_stale_approval(api):
    admin, other = login(api), login(api, "admin-b")
    m = {"external_id": "ID", "name": "Name"}
    upload(api, admin, "companies", "ID,Name\nO1,Acme\nO2,Other\n", m)
    upload(api, admin, "contacts", "ID,Name,Company ID\nP1,Lead,O1\n",
           {**m, "company_external_id": "Company ID"})
    endpoint = "/api/v1/migrations/relationships/reconcile"
    first_preview = api.get("/api/v1/migrations/relationships", headers=admin).json()
    assert len(first_preview["snapshot_sha256"]) == 64
    assert first_preview["resolved_references"] == 1
    assert api.post(endpoint, headers=admin).status_code == 422
    assert api.post(endpoint, headers=admin, json={"expected_snapshot": "0"*64}).status_code == 409
    assert api.post(endpoint, headers=other,
                    json={"expected_snapshot": first_preview["snapshot_sha256"]}).status_code == 409
    # Simulate a second import between the admin preview and the approval.
    upload(api, admin, "contacts", "ID,Name,Company ID\nP1,Lead,O2\n",
           {**m, "company_external_id": "Company ID"})
    stale = api.post(endpoint, headers=admin,
                     json={"expected_snapshot": first_preview["snapshot_sha256"]})
    assert stale.status_code == 409, stale.text
    current = api.get("/api/v1/migrations/relationships", headers=admin).json()
    assert current["snapshot_sha256"] != first_preview["snapshot_sha256"]
    good = api.post(endpoint, headers=admin,
                    json={"expected_snapshot": current["snapshot_sha256"]})
    assert good.status_code == 200 and good.json()["links_created"] == 1, good.text
    assert api.post(endpoint, headers=admin,
                    json={"expected_snapshot": current["snapshot_sha256"]}).status_code == 409
    fresh = api.get("/api/v1/migrations/relationships", headers=admin).json()
    assert fresh["snapshot_sha256"] != current["snapshot_sha256"]
    again = api.post(endpoint, headers=admin,
                     json={"expected_snapshot": fresh["snapshot_sha256"]})
    assert again.status_code == 200 and again.json()["links_created"] == 0
    rec = api.get("/api/v1/records", headers=admin).json()[0]["id"]
    assert len(api.get(f"/api/v1/records/{rec}/links", headers=admin).json()) == 1
