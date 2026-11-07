"""CRM Workspace: real imported data, tenant ACL, stages, modes and Focus results."""
import json
from .conftest import login

def load(api, h, kind, content, mapping):
    data = content.encode()
    preview = api.post("/api/v1/migrations/preview", headers=h,
        data={"kind":kind}, files={"file":("x.csv",data)})
    assert preview.status_code == 200, preview.text
    spec = {"kind":kind,"source":"pipedrive","columns":mapping,"digest":preview.json()["digest"]}
    response = api.post("/api/v1/migrations/commit", headers=h,
        data={"spec":json.dumps(spec)}, files={"file":("x.csv",data)})
    assert response.status_code == 200, response.text

def test_pipeline_and_account360_respect_company_mode_and_record_acl(api):
    admin, other = login(api), login(api, "admin-b")
    rep = login(api, "rep-a")
    mapping = {"external_id":"ID","name":"Name"}
    load(api, admin, "companies", "ID,Name\nO1,Acme\n", mapping)
    load(api, admin, "contacts", "ID,Name,Company ID\nP1,Alice,O1\n",
         {**mapping,"company_external_id":"Company ID"})
    load(api, admin, "deals", "ID,Name,Stage,Company ID,Contact ID\nD1,Deal A,Qualified,O1,P1\nD2,Deal B,Qualified,O1,P1\n",
         {**mapping,"stage":"Stage","company_external_id":"Company ID","contact_external_id":"Contact ID"})
    fingerprint = api.get("/api/v1/migrations/relationships", headers=admin).json()["snapshot_sha256"]
    assert api.post("/api/v1/migrations/relationships/reconcile", headers=admin,
                    json={"expected_snapshot":fingerprint}).status_code == 200
    records = api.get("/api/v1/records?kind=deals", headers=admin).json()
    first = next(r["id"] for r in records if r["external_id"] == "D1")
    second = next(r["id"] for r in records if r["external_id"] == "D2")
    rep_id = next(r["id"] for r in api.get("/api/v1/admin/reps", headers=admin).json()
                  if r["username"] == "rep-a")
    assert len(api.get("/api/v1/pipeline/board", headers=admin).json()["unmapped"]) == 2
    assert api.get("/api/v1/pipeline/board", headers=rep).status_code == 403
    stages = ["Qualified","Proposal","Won"]
    assert api.put("/api/v1/pipeline/stages", headers=rep, json={"stages":stages}).status_code == 403
    assert api.put("/api/v1/pipeline/stages", headers=admin, json={"stages":stages}).status_code == 200
    assert api.get("/api/v1/pipeline/stages", headers=other).json()["stages"] == []
    assert api.put("/api/v1/pipeline/stages", headers=admin, json={"stages":["Qualified","qualified"]}).status_code == 422
    assert api.put("/api/v1/pipeline/stages", headers=admin, json={"stages":["Won"]}).status_code == 409
    assert api.get(f"/api/v1/crm/{first}/360", headers=rep).status_code == 404
    assert api.get(f"/api/v1/crm/{first}/360", headers=other).status_code == 404
    grant = api.put(f"/api/v1/admin/records/{first}/access", headers=admin,json={"user_id":rep_id})
    assert grant.status_code == 200, grant.text
    assert api.get(f"/api/v1/crm/{second}/360", headers=rep).status_code == 404
    assert api.get(f"/api/v1/crm/{first}/360", headers=rep).json()["related"] == []
    assert len(api.get(f"/api/v1/crm/{first}/360", headers=admin).json()["related"]) == 2
    assert api.patch(f"/api/v1/pipeline/deals/{first}/stage", headers=rep,
                     json={"stage":"Won","expected_stage":"Qualified"}).status_code == 403
    r = api.patch(f"/api/v1/pipeline/deals/{first}/stage", headers=admin,
                  json={"stage":"Proposal","expected_stage":"Qualified"})
    assert r.status_code == 200 and r.json()["changed"]
    assert api.patch(f"/api/v1/pipeline/deals/{first}/stage", headers=admin,
                     json={"stage":"Won","expected_stage":"Qualified"}).status_code == 409
    weights = {"overdue":5,"inbound":15,"proposal":10,"stale":5}
    p = api.put("/api/v1/work/policy", headers=admin,
        json={"mode":"hybrid","timezone":"Europe/Madrid","weights":weights})
    assert p.status_code == 200, p.text
    board = api.get("/api/v1/pipeline/board", headers=rep).json()
    assert sum(len(lane["deals"]) for lane in board["lanes"]) == 1
    assert board["lanes"][1]["deals"][0]["id"] == first
    assert api.patch(f"/api/v1/pipeline/deals/{second}/stage", headers=rep,
                     json={"stage":"Won","expected_stage":"Qualified"}).status_code == 404
    assert api.patch(f"/api/v1/pipeline/deals/{first}/stage", headers=rep,
                     json={"stage":"Won","expected_stage":"Proposal"}).status_code == 200
    assert api.patch(f"/api/v1/pipeline/deals/{first}/stage", headers=rep,
                     json={"stage":"Won","expected_stage":"Won"}).json()["changed"] is False
    assert api.get(f"/api/v1/crm/{first}/360", headers=rep).json()["record"]["stage"] == "Won"
    assert api.put("/api/v1/work/policy", headers=admin,
        json={"mode":"guided","timezone":"Europe/Madrid","weights":weights}).status_code == 200
    assert api.get("/api/v1/pipeline/board", headers=rep).status_code == 403
    assert api.get(f"/api/v1/crm/{first}/360", headers=rep).status_code == 200
    assert api.get("/crm").status_code == 200

def test_account360_next_commitment_and_result_are_persisted(api):
    admin, rep = login(api), login(api, "rep-a")
    load(api, admin, "contacts", "ID,Name\nP1,Lead\n",
         {"external_id":"ID","name":"Name"})
    rid = api.get("/api/v1/records", headers=admin).json()[0]["id"]
    rep_id = next(r["id"] for r in api.get("/api/v1/admin/reps", headers=admin).json()
                  if r["username"] == "rep-a")
    task = api.post("/api/v1/work/tasks", headers=admin,
        json={"owner_id":rep_id,"record_id":rid,"title":"Call lead",
              "channel":"call","due_at":"2025-01-01T10:00:00Z",
              "idem":"crm-workspace-task-once-123"})
    assert task.status_code == 200, task.text
    overview = api.get(f"/api/v1/crm/{rid}/360", headers=rep).json()
    assert overview["next_commitment"]["title"] == "Call lead"
    assert overview["next_commitment"]["basis"] == "work_items.due_at"
    assert api.post("/api/v1/work/claim/" + task.json()["id"], headers=rep).status_code == 200
    r = api.post("/api/v1/work/complete", headers=rep, json={
        "task_id":task.json()["id"],"idem":"workspace-task-complete-idem-1",
        "outcome":"done","note":"Customer called"})
    assert r.status_code == 200, r.text
    after = api.get(f"/api/v1/crm/{rid}/360", headers=rep).json()
    assert after["next_commitment"] is None
    assert after["recent_results"][0]["note"] == "Customer called"
    assert after["recent_results"][0]["source"] == "work_results"
