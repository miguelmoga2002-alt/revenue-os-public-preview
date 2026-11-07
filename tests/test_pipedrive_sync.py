"""Offline Pipedrive v2 adapter and authenticated sync tests: no provider credentials."""
import json
from datetime import datetime, timezone
import httpx
from sqlalchemy import select
from .conftest import login
from app.models import CRMRecord, CRMSyncCheckpoint
from app import pipedrive_sync as pd


def enabled(monkeypatch):
 monkeypatch.setenv("REVENUE_OS_ENABLE_PIPEDRIVE_SYNC","1")
 monkeypatch.setenv("REVENUE_OS_PIPEDRIVE_CONNECTIONS_JSON",
                   json.dumps({"a":{"subdomain":"synthetic-test","token_env":"REVENUE_OS_PD_TOKEN_TEST"}}))
 monkeypatch.setenv("REVENUE_OS_PD_TOKEN_TEST","only-synthetic-token-1234567890")


def fake_record(external="101", name="Example",kind="companies"):
 return (external,{"name":name,"email":None,"phone":None,"company_external_id":None,
  "contact_external_id":None,"deal_external_id":None,"stage":None,"owner":None,
  "amount":None,"due_at":None,
  "attributes":json.dumps({"origin":"pipedrive_api_v2","update_time":None,
   "deal_status":None,"stage_id":None,"activity_done":None},sort_keys=True,separators=(',',':'))})


def test_sync_disabled_by_default_and_acl(api,monkeypatch):
 monkeypatch.delenv("REVENUE_OS_ENABLE_PIPEDRIVE_SYNC",raising=False)
 admin,rep,foreign=login(api),login(api,"rep-a"),login(api,"admin-b")
 assert api.get("/api/v1/integrations/pipedrive/status").status_code==401
 assert api.get("/api/v1/integrations/pipedrive/status",headers=rep).status_code==403
 assert api.post("/api/v1/integrations/pipedrive/sync",headers=admin,
   json={"kind":"deals","confirm":True}).status_code==503
 enabled(monkeypatch)
 assert not api.get("/api/v1/integrations/pipedrive/status",headers=foreign).json()["connection_configured"]
 assert api.post("/api/v1/integrations/pipedrive/sync",headers=foreign,
   json={"kind":"deals","confirm":True}).status_code==503
 assert api.post("/api/v1/integrations/pipedrive/sync",headers=rep,
   json={"kind":"deals","confirm":True}).status_code==403
 assert api.post("/api/v1/integrations/pipedrive/sync",headers=admin,
   json={"kind":"unknown","confirm":True}).status_code==422


def test_real_adapter_uses_get_header_cursor_and_bounded_url(monkeypatch):
 enabled(monkeypatch)
 observed=[]
 def handler(request):
  observed.append((str(request.url),request.headers.get("x-api-token"),request.method))
  assert request.url.host=="synthetic-test.pipedrive.com"
  assert request.url.path=="/api/v2/persons"
  assert "api_token=" not in str(request.url)
  assert "updated_until=" in str(request.url)
  if len(observed)==1:
   return httpx.Response(200,json={"success":True,"data":[{"id":31,"name":"Ada",
     "emails":[{"value":"ada@example.invalid"}],"org_id":45}],
     "additional_data":{"next_cursor":"opaque-token"}})
  assert request.url.params["cursor"]=="opaque-token"
  return httpx.Response(200,json={"success":True,"data":[{"id":32,"name":"Grace"}],
    "additional_data":{"next_cursor":None}})
 actual_client=httpx.Client
 transport=httpx.MockTransport(handler)
 monkeypatch.setattr(pd.httpx,"Client",lambda **kwargs:actual_client(transport=transport,**kwargs))
 rows=pd.pull_pages("synthetic-test","only-synthetic-token-1234567890","contacts",
  None,datetime.now(timezone.utc))
 assert len(rows)==2 and rows[0][1]["email"]=="ada@example.invalid"
 assert rows[0][1]["company_external_id"]=="45"
 assert all(x[2]=="GET" for x in observed)


def test_preview_commit_incremental_dedupe_and_provenance(api,monkeypatch):
 enabled(monkeypatch)
 calls=[]
 def pull(domain,token,kind,since,until):
  calls.append((kind,since,until))
  assert domain=="synthetic-test"
  return [fake_record("91","First")]
 monkeypatch.setattr(pd,"pull_pages",pull)
 admin=login(api)
 endpoint="/api/v1/integrations/pipedrive/sync"
 preview=api.post(endpoint,headers=admin,json={"kind":"companies"})
 assert preview.status_code==200,preview.text
 assert preview.json()["preview"] and preview.json()["count"]==1
 assert api.get("/api/v1/records?kind=companies",headers=admin).json()==[]
 saved=api.post(endpoint,headers=admin,json={"kind":"companies","confirm":True})
 assert saved.status_code==200,saved.text
 assert saved.json()["created"]==1
 status=api.get("/api/v1/integrations/pipedrive/status",headers=admin).json()
 assert status["checkpoints"]["companies"]["imported_count"]==1
 with api.app.state.test_session_factory() as db:
  records=db.scalars(select(CRMRecord).where(CRMRecord.tenant_id=="a")).all()
  assert len(records)==1
  assert records[0].source=="pipedrive"
  assert records[0].external_id=="91"
  assert json.loads(records[0].attributes)["origin"]=="pipedrive_api_v2"
  assert db.get(CRMSyncCheckpoint,("a","pipedrive","companies")) is not None
 # Prevent a near-immediate replay from doing another external call or write.
 repeat=api.post(endpoint,headers=admin,json={"kind":"companies","confirm":True})
 assert repeat.status_code==409
 assert len(calls)==2


def test_bounded_or_malformed_provider_responses_never_commit(api,monkeypatch):
 enabled(monkeypatch)
 admin=login(api)
 monkeypatch.setattr(pd,"pull_pages",lambda *args: (_ for _ in ()).throw(
  __import__('fastapi').HTTPException(502,"Respuesta CRM malformada")))
 assert api.post("/api/v1/integrations/pipedrive/sync",headers=admin,
  json={"kind":"contacts","confirm":True}).status_code==502
 with api.app.state.test_session_factory() as db:
  assert db.scalars(select(CRMSyncCheckpoint)).all()==[]
 assert api.get("/api/v1/records",headers=admin).json()==[]


def test_pipedrive_normalization_does_not_invent_stages_or_meetings():
 external,deal=pd.normalize("deals",{"id":123,"title":"Example deal","status":"open",
   "stage_id":8,"value":4200,"owner_id":7,"org_id":9})
 assert external=="123" and deal["stage"]=="pipedrive_stage_id:8"
 assert deal["company_external_id"]=="9"
 assert json.loads(deal["attributes"])["deal_status"]=="open"
 ext,activity=pd.normalize("activities",{"id":12,"subject":"Meeting booking",
   "done":True,"person_id":77,"due_date":"2026-11-20"})
 assert ext=="12" and json.loads(activity["attributes"])["activity_done"] is True
 assert activity["contact_external_id"]=="77"
 assert "meeting_held" not in str(activity)


def test_synced_deal_is_read_only_in_local_pipeline(api,monkeypatch):
 enabled(monkeypatch)
 admin=login(api)
 external,values=pd.normalize("deals",{"id":204,"title":"Synced Opportunity",
   "stage_id":8,"status":"open","value":900})
 monkeypatch.setattr(pd,"pull_pages",lambda *args:[(external,values)])
 result=api.post("/api/v1/integrations/pipedrive/sync",headers=admin,
    json={"kind":"deals","confirm":True})
 assert result.status_code==200,result.text
 record=next(r for r in api.get("/api/v1/records?kind=deals",headers=admin).json()
             if r["external_id"]=="204")
 assert api.put("/api/v1/pipeline/stages",headers=admin,json={
  "stages":["pipedrive_stage_id:8","Won"]}).status_code==200
 change=api.patch("/api/v1/pipeline/deals/"+record["id"]+"/stage",headers=admin,
  json={"stage":"Won","expected_stage":"pipedrive_stage_id:8"})
 assert change.status_code==409
 after=api.get("/api/v1/crm/"+record["id"]+"/360",headers=admin).json()
 assert after["record"]["stage"]=="pipedrive_stage_id:8"


def test_sync_does_not_overwrite_csv_record_with_same_external_source(api,monkeypatch):
 enabled(monkeypatch)
 admin=login(api)
 from .test_crm_workspace import load
 load(api,admin,"companies","ID,Name\n91,Original CSV\n",{"external_id":"ID","name":"Name"})
 # The CSV loader uses source=pipedrive, so the connector must fail closed.
 monkeypatch.setattr(pd,"pull_pages",lambda *args:[fake_record("91","Updated via API")])
 response=api.post("/api/v1/integrations/pipedrive/sync",headers=admin,
   json={"kind":"companies","confirm":True})
 assert response.status_code==409,response.text
 rows=api.get("/api/v1/records?kind=companies",headers=admin).json()
 assert len(rows)==1 and rows[0]["name"]=="Original CSV"
 with api.app.state.test_session_factory() as db:
  assert db.scalars(select(CRMSyncCheckpoint)).all()==[]
