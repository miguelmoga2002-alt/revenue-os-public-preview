"""Deal Workspace: persisted notes, timeline, ACL and idempotent mutations."""
from datetime import datetime, timezone, timedelta
from .conftest import login
from .test_crm_workspace import load


def seeded_deal(api):
 admin=login(api)
 load(api,admin,"deals","ID,Name,Stage\nD1,Deal Demo,Qualified\nD2,Private Deal,Qualified\n",
      {"external_id":"ID","name":"Name","stage":"Stage"})
 rows=api.get("/api/v1/records?kind=deals",headers=admin).json()
 return admin,{row["external_id"]:row["id"] for row in rows}


def test_notes_acl_idempotency_and_no_rollback_after_note(api):
 admin,ids=seeded_deal(api)
 rep=login(api,"rep-a")
 other=login(api,"admin-b")
 d=ids["D1"]
 endpoint=f"/api/v1/deals/{d}/notes"
 payload={"text":"Necesidad: confirmar CRM actual","idem":"note-demo-once-0001"}
 assert api.get(f"/api/v1/deals/{d}/timeline",headers=rep).status_code==404
 assert api.post(endpoint,headers=other,json=payload).status_code==404
 assert api.post(endpoint,headers=rep,json=payload).status_code==404
 assert api.post(endpoint,headers=admin,json={"text":"x","idem":"note-demo-tiny"}).status_code==422
 assert api.post(endpoint,headers=admin,json={"text":"x"*3001,"idem":"note-demo-large"}).status_code==422
 assert api.post(endpoint,headers=admin,json={"text":"Valid but missing idempotency"}).status_code==422
 first=api.post(endpoint,headers=admin,json=payload)
 assert first.status_code==200,first.text
 assert first.json()["already_recorded"] is False
 duplicate=api.post(endpoint,headers=admin,json=payload)
 assert duplicate.status_code==200 and duplicate.json()["already_recorded"]
 assert duplicate.json()["note"]["id"]==first.json()["note"]["id"]
 assert api.post(endpoint,headers=admin,json={**payload,"text":"Changed text"}).status_code==409
 assert api.post(f"/api/v1/deals/{ids['D2']}/notes",headers=admin,json=payload).status_code==409
 overview=api.get(f"/api/v1/deals/{d}/timeline",headers=admin).json()
 assert overview["events"][0]["kind"]=="note"
 assert sum(x["kind"]=="note" for x in overview["events"])==1
 assert overview["events"][0]["verification"]=="user_attested"
 assert any(x["kind"]=="created" for x in overview["events"])
 # Assigning access to a deal does not expose other deals.
 rep_id=next(x["id"] for x in api.get("/api/v1/admin/reps",headers=admin).json()
             if x["username"]=="rep-a")
 assert api.put(f"/api/v1/admin/records/{d}/access",headers=admin,
                json={"user_id":rep_id}).status_code==200
 visible=api.get(f"/api/v1/deals/{d}/timeline",headers=rep)
 assert visible.status_code==200
 assert visible.json()["events"][0]["body"]=="Necesidad: confirmar CRM actual"
 assert api.get(f"/api/v1/deals/{ids['D2']}/timeline",headers=rep).status_code==404
 bad=api.get(f"/api/v1/deals/{d}/timeline",headers=other)
 assert bad.status_code==404
 # Immutable note blocks data erasure through migration rollback.
 batch=next(x for x in api.get("/api/v1/migrations/batches",headers=admin).json()
            if x["kind"]=="deals" and x["status"]=="complete")
 failed=api.post(f"/api/v1/migrations/{batch['id']}/rollback",headers=admin)
 assert failed.status_code==409
 assert "notas" in failed.json()["detail"]
 assert api.get(f"/api/v1/deals/{d}/timeline",headers=admin).status_code==200


def test_timeline_stage_tasks_outcomes_and_manual_signal_are_grounded(api):
 admin,ids=seeded_deal(api)
 d=ids["D1"]
 assert api.put("/api/v1/pipeline/stages",headers=admin,
                json={"stages":["Qualified","Proposal"]}).status_code==200
 stage=api.patch(f"/api/v1/pipeline/deals/{d}/stage",headers=admin,
     json={"stage":"Proposal","expected_stage":"Qualified"})
 assert stage.status_code==200,stage.text
 rep=login(api,"rep-a")
 rep_id=next(x["id"] for x in api.get("/api/v1/admin/reps",headers=admin).json()
             if x["username"]=="rep-a")
 task=api.post("/api/v1/work/tasks",headers=admin,json={
     "owner_id":rep_id,"record_id":d,"title":"Llamar para discovery",
     "channel":"call","due_at":"2025-01-01T10:00:00Z",
     "idem":"workspace-deal-task-12345"})
 assert task.status_code==200,task.text
 claim=api.post("/api/v1/work/claim/"+task.json()["id"],headers=rep)
 assert claim.status_code==200,claim.text
 complete=api.post("/api/v1/work/complete",headers=rep,json={
    "task_id":task.json()["id"],"idem":"workspace-deal-result-123",
    "outcome":"contacted","note":"Declaró interés en piloto",
    "next_title":"Enviar propuesta",
    "next_due_at":(datetime.now(timezone.utc)+timedelta(days=1)).isoformat()})
 assert complete.status_code==200,complete.text
 manual=api.post("/api/v1/signals/reports",headers=rep,json={
    "record_id":d,"kind":"meeting_held","detail":"Reunión declarada, sin integración",
    "occurred_at":datetime.now(timezone.utc).isoformat(),
    "idem":"workspace-manual-signal-123"})
 assert manual.status_code==200,manual.text
 result=api.get(f"/api/v1/deals/{d}/timeline",headers=admin)
 assert result.status_code==200,result.text
 rows=result.json()["events"]
 assert {"created","stage","task","result","signal"}.issubset({e["kind"] for e in rows})
 assert any(e["kind"]=="stage" and "Qualified → Proposal"==e["body"] for e in rows)
 assert any(e["kind"]=="task" and e["verification"]=="planned_not_executed"
            and e["headline"].startswith("Tarea asignada:") for e in rows)
 assert any(e["kind"]=="result" and e["body"]=="Declaró interés en piloto" for e in rows)
 assert any(e["kind"]=="signal" and e["verification"]=="user_attested" for e in rows)
 assert all(e["source"] and e["verification"] and e["at"] for e in rows)
 # Read access to this deal does not grant every other opportunity.
 rep_result=api.get(f"/api/v1/deals/{d}/timeline",headers=rep)
 assert rep_result.status_code==200
 assert any(e["kind"]=="result" for e in rep_result.json()["events"])
 assert api.get(f"/api/v1/deals/{ids['D2']}/timeline",headers=rep).status_code==404


def test_contact_is_not_a_deal(api):
 admin=login(api)
 load(api,admin,"contacts","ID,Name\nP1,Person\n",
      {"external_id":"ID","name":"Name"})
 rid=api.get("/api/v1/records?kind=contacts",headers=admin).json()[0]["id"]
 assert api.get(f"/api/v1/deals/{rid}/timeline",headers=admin).status_code==404
 assert api.post(f"/api/v1/deals/{rid}/notes",headers=admin,
     json={"text":"Should not attach here","idem":"note-wrong-kind-123"}).status_code==404
