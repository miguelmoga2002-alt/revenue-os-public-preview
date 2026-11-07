"""Offline HubSpot/Salesforce OAuth CRM migration contract tests."""
import json
from datetime import datetime,timezone
import httpx
from cryptography.fernet import Fernet
from sqlalchemy import select
from app.models import CRMRecord,CRMOAuthConnection,CRMSyncCheckpoint
from app import crm_multi_sync as multi
from app import crm_oauth as oauth
from .conftest import login

def config(monkeypatch):
 monkeypatch.setenv('REVENUE_OS_OAUTH_ENCRYPTION_KEY',Fernet.generate_key().decode())
 monkeypatch.setenv('REVENUE_OS_ENABLE_CRM_OAUTH','1')
 monkeypatch.setenv('REVENUE_OS_OAUTH_CALLBACK_URL','https://sandbox.revenue.example.invalid/api/v1/oauth/callback')
 for provider in ('HUBSPOT','SALESFORCE','PIPEDRIVE'):
  monkeypatch.setenv('REVENUE_OS_'+provider+'_CLIENT_ID','synthetic-client-'+provider)
  monkeypatch.setenv('REVENUE_OS_'+provider+'_CLIENT_SECRET','synthetic-secret-'+provider)

def establish(api,provider):
 with api.app.state.test_session_factory.begin() as db:
  db.add(CRMOAuthConnection(tenant_id='a',provider=provider,account_key='synthetic',
    token_ciphertext=oauth.encrypt('synthetic-bearer-access-token-123456'),
    refresh_ciphertext=None,api_base={
     'hubspot':'https://api.hubapi.com','salesforce':'https://synthetic.my.salesforce.com',
     'pipedrive':'https://synthetic.pipedrive.com'}[provider]))

def test_hubspot_contract_paging_and_fields(monkeypatch):
 observed=[]
 orig=httpx.Client
 def handler(request):
  observed.append(request)
  assert request.url.host=='api.hubapi.com'
  assert request.url.path=='/crm/objects/2026-03/contacts'
  assert request.headers['Authorization'].startswith('Bearer ')
  if len(observed)==1:
   return httpx.Response(200,json={'results':[{'id':'123','properties':{
    'firstname':'Ada','lastname':'Lovelace','email':'ada@example.invalid'}}],
    'paging':{'next':{'after':'next-opaque'}}})
  assert request.url.params['after']=='next-opaque'
  return httpx.Response(200,json={'results':[{'id':'124','properties':{
   'firstname':'Grace','lastname':'Hopper'}}]})
 monkeypatch.setattr(multi.httpx,'Client',lambda **kwargs:orig(transport=httpx.MockTransport(handler),**kwargs))
 rows=multi.pull_pages('hubspot','https://api.hubapi.com','synthetic-token','contacts',
  None,datetime.now(timezone.utc))
 assert [r[0] for r in rows]==['123','124']
 assert rows[0][1]['name']=='Ada Lovelace'
 assert rows[0][1]['email']=='ada@example.invalid'
 assert rows[0][1]['company_external_id'] is None

def test_salesforce_query_cursor_and_host_validation(monkeypatch):
 observed=[]
 orig=httpx.Client
 def handler(request):
  observed.append(request)
  assert request.url.host=='synthetic.my.salesforce.com'
  assert request.headers['Authorization'].startswith('Bearer ')
  if len(observed)==1:
   assert request.url.path=='/services/data/v66.0/query'
   assert 'FROM Opportunity' in request.url.params['q']
   return httpx.Response(200,json={'records':[{'Id':'006000abc','Name':'Deal A',
    'StageName':'Prospecting','AccountId':'001000abc','Amount':3500}],
    'done':False,'nextRecordsUrl':'/services/data/v66.0/query/01gABC123'})
  assert request.url.path=='/services/data/v66.0/query/01gABC123'
  return httpx.Response(200,json={'records':[{'Id':'006000def','Name':'Deal B',
    'StageName':'Closed Won'}],'done':True})
 monkeypatch.setattr(multi.httpx,'Client',lambda **kwargs:orig(transport=httpx.MockTransport(handler),**kwargs))
 rows=multi.pull_pages('salesforce','https://synthetic.my.salesforce.com',
  'synthetic-token','deals',None,datetime.now(timezone.utc))
 assert len(rows)==2 and rows[0][1]['company_external_id']=='001000abc'
 assert rows[0][1]['stage']=='salesforce_stage:Prospecting'
 assert rows[1][1]['stage']=='salesforce_stage:Closed Won'

def test_oauth_sync_preview_commit_and_cross_tenant(api,monkeypatch):
 config(monkeypatch)
 establish(api,'hubspot')
 admin,foreign,rep=login(api),login(api,'admin-b'),login(api,'rep-a')
 called=[]
 def adapter(provider,base,token,kind,since,until):
  called.append((provider,kind,since))
  assert provider=='hubspot' and token=='synthetic-bearer-access-token-123456'
  return [multi.normalized('hubspot','contacts',{'id':'900','properties':{
   'firstname':'Imported','lastname':'Person','email':'imported@example.invalid'}})]
 monkeypatch.setattr(multi,'pull_pages',adapter)
 endpoint='/api/v1/integrations/crm/sync'
 assert api.get('/api/v1/integrations/crm/providers',headers=rep).status_code==403
 assert api.post(endpoint,headers=foreign,json={'provider':'hubspot','kind':'contacts','confirm':True}).status_code==409
 preview=api.post(endpoint,headers=admin,json={'provider':'hubspot','kind':'contacts'})
 assert preview.status_code==200 and preview.json()['count']==1
 assert preview.json()['incremental_filter_applied'] is False
 assert api.get('/api/v1/records?kind=contacts',headers=admin).json()==[]
 saved=api.post(endpoint,headers=admin,json={'provider':'hubspot','kind':'contacts','confirm':True})
 assert saved.status_code==200,saved.text
 assert saved.json()['created']==1
 with api.app.state.test_session_factory() as db:
  row=db.scalar(select(CRMRecord).where(CRMRecord.source=='hubspot'))
  assert row and row.external_id=='900'
  assert json.loads(row.attributes)['origin']=='hubspot_oauth'
  checkpoint=db.get(CRMSyncCheckpoint,('a','hubspot','contacts'))
  assert checkpoint and checkpoint.imported_count==1
 assert api.get('/api/v1/records?kind=contacts',headers=foreign).json()==[]
 repeated=api.post(endpoint,headers=admin,json={'provider':'hubspot','kind':'contacts','confirm':True})
 assert repeated.status_code==200 and repeated.json()['unchanged']==1

def test_salesforce_invalid_cursor_rejected():
 import pytest
 from fastapi import HTTPException
 with pytest.raises(HTTPException) as err:
  multi.normalized('salesforce','deals',{'Id':'x','Name':None})
 assert err.value.status_code==502
 assert multi.normalized('salesforce','activities',{'Id':'00T001','Subject':'Call logged',
  'Status':'Completed','WhatId':'006other'})[1]['deal_external_id'] is None
