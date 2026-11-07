"""Rep-owned tasks from a granted deal: tenancy, CAS, receipts, and audit."""
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from .conftest import login
from .test_deal_workspace import seeded_deal


def future(hours=3):
    return (datetime.now(timezone.utc)+timedelta(hours=hours)).isoformat()


def grant(api, admin, deal, username="rep-a"):
    owner = next(row["id"] for row in api.get(
        "/api/v1/admin/reps", headers=admin).json() if row["username"] == username)
    r = api.put(f"/api/v1/admin/records/{deal}/access", headers=admin,
                json={"user_id":owner})
    assert r.status_code == 200, r.text
    return owner


def test_rep_creates_own_followup_only_for_granted_deal(api):
    admin, deals = seeded_deal(api)
    granted, private = deals["D1"], deals["D2"]
    rep = login(api, "rep-a")
    other = login(api, "admin-b")
    endpoint = f"/api/v1/deals/{granted}/my-tasks"
    owner = next(row["id"] for row in api.get(
        "/api/v1/admin/reps", headers=admin).json() if row["username"] == "rep-a")
    payload = {"idem":"rep-followup-123456", "title":"Preparar propuesta",
               "channel":"email", "due_at":future()}
    assert api.get(endpoint, headers=rep).status_code == 404
    assert api.post(endpoint, headers=rep, json=payload).status_code == 404
    assert api.post(f"/api/v1/deals/{private}/my-tasks", headers=rep,
                    json=payload).status_code == 404
    assert api.post(endpoint, headers=other, json=payload).status_code == 403
    grant(api, admin, granted)
    assert api.post(endpoint, headers=admin, json=payload).status_code == 403
    first = api.post(endpoint, headers=rep, json=payload)
    assert first.status_code == 200, first.text
    assert first.json()["already_recorded"] is False
    replay = api.post(endpoint, headers=rep, json=payload)
    assert replay.status_code == 200 and replay.json()["already_recorded"]
    assert replay.json()["id"] == first.json()["id"]
    # Idempotency must remain valid after the scheduling window has passed.
    from unittest.mock import patch
    with patch("app.deal_actions.utcnow",
               return_value=datetime.now(timezone.utc)+timedelta(days=10)):
        late = api.post(endpoint, headers=rep, json=payload)
    assert late.status_code == 200 and late.json()["already_recorded"]
    changed = api.post(endpoint, headers=rep, json={**payload, "title":"Otro intento"})
    assert changed.status_code == 409
    invalid = api.post(endpoint, headers=rep, json={**payload,
                      "idem":"rep-followup-invalid", "due_at":future(-2)})
    assert invalid.status_code == 422
    listed = api.get(endpoint, headers=rep)
    assert listed.status_code == 200
    assert len(listed.json()["items"]) == 1
    assert listed.json()["items"][0]["id"] == first.json()["id"]
    assert api.get(f"/api/v1/deals/{private}/my-tasks", headers=rep).status_code == 404
    # Neither creation nor rescheduling changes the imported owner on the deal.
    assert api.get(f"/api/v1/crm/{granted}/360",headers=rep).json()["record"]["owner"] is None


def test_rep_reschedule_compare_and_swap_idempotency_and_timeline(api):
    admin,deals = seeded_deal(api)
    d=deals["D1"]
    grant(api,admin,d)
    rep = login(api,"rep-a")
    endpoint=f"/api/v1/deals/{d}/my-tasks"
    source=future(3)
    original=api.post(endpoint,headers=rep,json={
        "idem":"rep-schedule-initial-123", "title":"Enviar material",
        "channel":"email", "due_at":source})
    assert original.status_code==200,original.text
    task=original.json()["id"]
    first_due=api.get(endpoint,headers=rep).json()["items"][0]["due_at"]
    new_due=future(4)
    change={"idem":"rep-replan-idem-12345","expected_due_at":first_due,
            "due_at":new_due}
    route=f"{endpoint}/{task}"
    first=api.patch(route,headers=rep,json=change)
    assert first.status_code==200,first.text
    assert first.json()["changed"] and not first.json()["already_recorded"]
    replay=api.patch(route,headers=rep,json=change)
    assert replay.status_code==200 and replay.json()["already_recorded"]
    assert replay.json()["due_at"]==first.json()["due_at"]
    from unittest.mock import patch
    with patch("app.deal_actions.utcnow",
               return_value=datetime.now(timezone.utc)+timedelta(days=10)):
        late = api.patch(route, headers=rep, json=change)
    assert late.status_code == 200 and late.json()["already_recorded"]
    assert api.patch(route,headers=rep,json={
        **change,"due_at":future(5)}).status_code==409
    assert api.patch(route,headers=rep,json={
        **change,"idem":"rep-replan-stale-1234"}).status_code==409
    assert api.patch(route,headers=rep,json={
        **change,"idem":"rep-replan-past-1234","due_at":future(-1)}).status_code==422
    assert api.patch(f"{endpoint}/{uuid4()}",headers=rep,json=change).status_code==404
    assert api.patch(f"/api/v1/deals/{deals['D2']}/my-tasks/{task}",
                     headers=rep,json=change).status_code==404
    timeline=api.get(f"/api/v1/deals/{d}/timeline",headers=rep).json()["events"]
    assert any(x["kind"]=="task" and x["verification"]=="planned_not_executed"
               and x["headline"].startswith("Tarea propia programada:")
               for x in timeline)
    replans=[x for x in timeline if x["kind"]=="reschedule"]
    assert len(replans)==1
    assert replans[0]["related_id"]==task
    assert replans[0]["source"]=="audit_events"
    assert replans[0]["verification"]=="recorded_change"
    assert " → " in replans[0]["body"]
    # Replan audit is independently persisted and does not duplicate on retries.
    with api.app.state.test_session_factory() as db:
        from app.models import Audit
        from sqlalchemy import select
        audits=db.scalars(select(Audit).where(Audit.action=="task.replan",
                      Audit.target==d)).all()
        assert len(audits)==1


def test_rep_cannot_reschedule_active_manager_task(api):
    admin,deals=seeded_deal(api)
    d=deals["D1"]
    owner=grant(api,admin,d)
    rep=login(api,"rep-a")
    t=api.post("/api/v1/work/tasks",headers=admin,json={
       "idem":"manager-activetask-123", "owner_id":owner,"record_id":d,
       "title":"Llamar ahora", "channel":"call",
       "due_at":future(-1)})
    assert t.status_code==200,t.text
    id=t.json()["id"]
    # Even an unclaimed overdue manager action cannot be postponed to skip Focus priority.
    cannot_skip=api.patch(f"/api/v1/deals/{d}/my-tasks/{id}",headers=rep,json={
       "idem":"skip-due-action-1234", "expected_due_at":future(-1),
       "due_at":future(5)})
    assert cannot_skip.status_code==409,cannot_skip.text
    assert api.post("/api/v1/work/claim/"+id,headers=rep).status_code==200
    response=api.patch(f"/api/v1/deals/{d}/my-tasks/{id}",headers=rep,json={
       "idem":"replan-active-123456", "expected_due_at":future(-1),
       "due_at":future(5)})
    assert response.status_code==409,response.text
