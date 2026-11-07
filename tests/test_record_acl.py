"""Record ACL regression: intra-tenant grants, revocation, links and cross-tenant blocks."""
from .conftest import login
import json
from sqlalchemy import select
from app.models import User


def upload(api, h, kind, csv_text, columns):
    data=csv_text.encode("utf-8")
    preview=api.post("/api/v1/migrations/preview",headers=h,data={"kind":kind},files={"file":("a.csv",data)})
    assert preview.status_code==200,preview.text
    spec={"kind":kind,"source":"pipedrive","columns":columns,"digest":preview.json()["digest"]}
    result=api.post("/api/v1/migrations/commit",headers=h,data={"spec":json.dumps(spec)},files={"file":("a.csv",data)})
    assert result.status_code==200,result.text


def test_rep_sees_only_explicitly_granted_records_and_links(api):
    a,b=login(api),login(api,"admin-b")
    rep_a,rep_b=login(api,"rep-a"),login(api,"rep-b")
    m={"external_id":"ID","name":"Name"}
    upload(api,a,"companies","ID,Name\nO1,Acme\n",m)
    upload(api,a,"contacts","ID,Name,Company ID\nP1,Alice,O1\nP2,Bob,O1\n",{**m,"company_external_id":"Company ID"})
    records=api.get("/api/v1/records",headers=a).json()
    p1=next(x["id"] for x in records if x["external_id"]=="P1")
    p2=next(x["id"] for x in records if x["external_id"]=="P2")
    company=api.get("/api/v1/records?kind=companies",headers=a).json()[0]["id"]
    assert api.get("/api/v1/records",headers=rep_a).json()==[]
    assert api.get("/api/v1/records/"+p1,headers=rep_a).status_code==404
    assert api.get("/api/v1/records/"+p1+"/links",headers=rep_a).status_code==404
    assert api.get("/api/v1/records/"+p1,headers=rep_b).status_code==404
    fingerprint=api.get("/api/v1/migrations/relationships",headers=a).json()["snapshot_sha256"]
    assert api.post("/api/v1/migrations/relationships/reconcile",headers=a,json={"expected_snapshot":fingerprint}).status_code==200
    # The existing rep identity comes from fixture, admin endpoint returns it.
    reps=api.get("/api/v1/admin/reps",headers=a).json()
    rep_id=next(x["id"] for x in reps if x["username"]=="rep-a")
    b_id=next(x["id"] for x in api.get("/api/v1/admin/reps",headers=b).json() if x["username"]=="rep-b")
    url="/api/v1/admin/records/"+p1+"/access"
    assert api.put(url,headers=rep_a,json={"user_id":rep_id}).status_code==403
    assert api.put(url,headers=a,json={"user_id":b_id}).status_code==404
    grant=api.put(url,headers=a,json={"user_id":rep_id})
    assert grant.status_code==200 and grant.json()["status"]=="granted"
    assert api.put(url,headers=a,json={"user_id":rep_id}).json()["status"]=="already_granted"
    assert [r["id"] for r in api.get("/api/v1/records",headers=rep_a).json()]==[p1]
    assert api.get("/api/v1/records/"+p2,headers=rep_a).status_code==404
    assert api.get("/api/v1/records/"+p1,headers=rep_a).status_code==200
    assert api.get("/api/v1/records/"+p1+"/links",headers=rep_a).json()==[]
    assert api.put("/api/v1/admin/records/"+company+"/access",headers=a,json={"user_id":rep_id}).status_code==200
    assert len(api.get("/api/v1/records/"+p1+"/links",headers=rep_a).json())==1
    assert api.delete(url+"/"+rep_id,headers=b).status_code==404
    assert api.delete(url+"/"+rep_id,headers=a).json()["status"]=="revoked"
    assert api.get("/api/v1/records/"+p1,headers=rep_a).status_code==404
    assert api.get("/api/v1/records",headers=rep_b).json()==[]


def test_task_assignment_grants_access_and_active_task_prevents_revoke(api):
    a=login(api)
    rep=login(api,"rep-a")
    upload(api,a,"contacts","ID,Name\nP10,Assigned lead\n",{"external_id":"ID","name":"Name"})
    record=api.get("/api/v1/records",headers=a).json()[0]["id"]
    rep_id=next(x["id"] for x in api.get("/api/v1/admin/reps",headers=a).json() if x["username"]=="rep-a")
    assert api.get("/api/v1/records/"+record,headers=rep).status_code==404
    result=api.post("/api/v1/work/tasks",headers=a,json={"owner_id":rep_id,"record_id":record,"title":"Call assigned lead","channel":"call","due_at":"2025-01-01T12:00:00Z","idem":"task-assigned-lead-123"})
    assert result.status_code==200,result.text
    assert api.get("/api/v1/records/"+record,headers=rep).status_code==200
    access=f"/api/v1/admin/records/{record}/access/{rep_id}"
    assert api.delete(access,headers=a).status_code==409
