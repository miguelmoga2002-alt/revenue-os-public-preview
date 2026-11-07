"""Next Best Action: verifiable, deterministic, tenant-scoped evidence."""
from datetime import datetime, timedelta, timezone

from .conftest import login


def make_task(api, admin, owner, *, minutes=-60, source="inbound", suffix="a"):
    return api.post("/api/v1/work/tasks", headers=admin, json={
        "idem": "nba-evidence-operation-" + suffix,
        "owner_id": owner,
        "title": "Llamar al contacto de pruebas",
        "channel": "call",
        "priority_source": source,
        "due_at": (datetime.now(timezone.utc) + timedelta(minutes=minutes)).isoformat(),
    })


def test_nba_reports_actual_sources_and_score_without_invented_signals(api):
    admin = login(api, "admin-a")
    rep = login(api, "rep-a")
    owner = next(r["id"] for r in api.get("/api/v1/admin/reps", headers=admin).json()
                 if r["username"] == "rep-a")
    created = make_task(api, admin, owner)
    assert created.status_code == 200, created.text
    queue = api.get("/api/v1/work/queue", headers=rep)
    assert queue.status_code == 200
    selected = queue.json()["ready"][0]
    assert selected["id"] == created.json()["id"]
    assert selected["recommendation"] == {
        "action": "Llamar al contacto de pruebas", "channel": "call",
        "basis": "assigned_work_item", "automatic_execution": False,
    }
    assert selected["evidence_scope"] == "assigned_task_only"
    assert selected["score_breakdown"]["base"] == 100
    assert selected["score_breakdown"]["overdue"] >= 20
    assert selected["score_breakdown"]["declared_priority"] == 15
    assert selected["score"] == sum(selected["score_breakdown"].values())
    assert selected["policy_revision"] == 1
    assert datetime.fromisoformat(selected["evaluated_at"]).tzinfo is not None
    evidence = selected["evidence"]
    assert [e["rule"] for e in evidence] == ["due_window", "declared_priority"]
    assert all(e["origin"] == "work_items" and
               datetime.fromisoformat(e["recorded_at"]).tzinfo is not None
               for e in evidence)
    assert evidence[1]["field"] == "priority_source"
    assert evidence[1]["value"] == "inbound"
    assert "No confirma" in evidence[1]["explanation"]
    assert "email" not in str(selected["recommendation"]).lower()
    assert api.post("/api/v1/work/claim/" + selected["id"], headers=rep).status_code == 200
    active = api.get("/api/v1/work/queue", headers=rep).json()
    assert active["locked"] is True
    assert active["active"]["evidence_scope"] == "assigned_task_only"
    assert active["active"]["score"] >= selected["score"]


def test_nba_classification_is_not_inferred_and_tenants_remain_isolated(api):
    admin = login(api, "admin-a")
    rep = login(api, "rep-a")
    other_rep = login(api, "rep-b")
    owner = next(r["id"] for r in api.get("/api/v1/admin/reps", headers=admin).json()
                 if r["username"] == "rep-a")
    manual = make_task(api, admin, owner, minutes=-5, source="manual", suffix="manual")
    unknown = make_task(api, admin, owner, minutes=-5, source="web-intent", suffix="unknown")
    assert manual.status_code == unknown.status_code == 200
    ready = api.get("/api/v1/work/queue", headers=rep).json()["ready"]
    assert len(ready) == 2
    for row in ready:
        assert row["score_breakdown"]["declared_priority"] == 0
        assert row["evidence_scope"] == "assigned_task_only"
    manual_row = next(r for r in ready if r["id"] == manual.json()["id"])
    unknown_row = next(r for r in ready if r["id"] == unknown.json()["id"])
    assert len(manual_row["evidence"]) == 1
    assert unknown_row["evidence"][1]["value"] == "web-intent"
    assert unknown_row["evidence"][1]["points"] == 0
    assert api.get("/api/v1/work/queue", headers=other_rep).json()["ready"] == []
    assert api.get("/api/v1/work/queue").status_code == 401
    weights = {"overdue": 0, "inbound": 100, "proposal": 0, "stale": 0}
    assert api.put("/api/v1/work/policy", headers=admin, json={
        "mode": "pipeline", "timezone": "Europe/Madrid", "weights": weights,
    }).status_code == 200
    assert api.get("/api/v1/work/queue", headers=rep).status_code == 403
