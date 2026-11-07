"""Synthetic OAuth PKCE session, encrypted-at-rest, tenant ACL and replay tests."""
from urllib.parse import urlparse,parse_qs
from cryptography.fernet import Fernet
from sqlalchemy import select
from .conftest import login
from app.models import CRMOAuthPending, CRMOAuthConnection
from app import crm_oauth as oauth

def configured(monkeypatch):
 monkeypatch.setenv('REVENUE_OS_ENABLE_CRM_OAUTH','1')
 monkeypatch.setenv('REVENUE_OS_OAUTH_ENCRYPTION_KEY',Fernet.generate_key().decode())
 monkeypatch.setenv('REVENUE_OS_OAUTH_CALLBACK_URL','https://sandbox.revenue.example.invalid/api/v1/oauth/callback')
 for p in ('PIPEDRIVE','HUBSPOT','SALESFORCE'):
  monkeypatch.setenv('REVENUE_OS_'+p+'_CLIENT_ID','synthetic-client-'+p)
  monkeypatch.setenv('REVENUE_OS_'+p+'_CLIENT_SECRET','synthetic-client-secret-'+p)

def test_oauth_disabled_and_not_for_sales_rep(api,monkeypatch):
 admin,rep=login(api),login(api,'rep-a')
 assert api.post('/api/v1/oauth/hubspot/start',headers=admin).status_code==503
 configured(monkeypatch)
 assert api.post('/api/v1/oauth/hubspot/start').status_code==401
 assert api.post('/api/v1/oauth/hubspot/start',headers=rep).status_code==403
 assert api.post('/api/v1/oauth/unknown/start',headers=admin).status_code==422
 assert api.get('/api/v1/oauth/connections',headers=rep).status_code==403
 assert api.get('/api/v1/oauth/callback').status_code==200

def test_oauth_three_providers_state_and_pkce(api,monkeypatch):
 configured(monkeypatch)
 admin=login(api)
 for provider in ('hubspot','salesforce','pipedrive'):
  response=api.post('/api/v1/oauth/'+provider+'/start',headers=admin)
  assert response.status_code==200,response.text
  url=urlparse(response.json()['authorization_url'])
  qs=parse_qs(url.query)
  assert qs['code_challenge_method']==['S256'] and qs['response_type']==['code']
  assert qs['state'] and qs['code_challenge']
  assert 'client_secret' not in qs
  assert url.hostname==oauth.OAUTH_HOSTS[provider]
  with api.app.state.test_session_factory() as db:
   rows=db.scalars(select(CRMOAuthPending).where(CRMOAuthPending.tenant_id=='a')).all()
   assert all(item.verifier_ciphertext.count('.')==0 for item in rows)
  assert api.post('/api/v1/oauth/complete',headers=admin,json={
   'provider':provider,'code':'fake-provider-code','state':'invalid-state-with-enough-characters'}).status_code==404

def test_oauth_tenant_isolation_complete_replay_disconnect(api,monkeypatch):
 configured(monkeypatch)
 admin,foreign=login(api),login(api,'admin-b')
 start=api.post('/api/v1/oauth/hubspot/start',headers=admin).json()
 state=parse_qs(urlparse(start['authorization_url']).query)['state'][0]
 # Valid tuple returned explicitly without depending on provider network.
 monkeypatch.setattr(oauth,'exchange',lambda *args:(
  'synthetic-access-token-123456','synthetic-refresh-token-123456',
  'https://api.hubapi.com',3600,'not_verified'))
 body={'provider':'hubspot','code':'synthetic-code-12345','state':state}
 assert api.post('/api/v1/oauth/complete',headers=foreign,json=body).status_code==404
 response=api.post('/api/v1/oauth/complete',headers=admin,json=body)
 assert response.status_code==200,response.text
 assert response.json()['connected'] is True
 assert api.post('/api/v1/oauth/complete',headers=admin,json=body).status_code==404
 with api.app.state.test_session_factory() as db:
  conn=db.get(CRMOAuthConnection,('a','hubspot'))
  assert conn and 'synthetic-access-token' not in conn.token_ciphertext
  assert oauth.decrypt(conn.token_ciphertext)=='synthetic-access-token-123456'
 assert api.get('/api/v1/oauth/connections',headers=foreign).json()==[]
 listing=api.get('/api/v1/oauth/connections',headers=admin).json()
 assert len(listing)==1 and 'token' not in str(listing).lower()
 assert api.delete('/api/v1/oauth/connections/hubspot',headers=foreign).status_code==404
 assert api.delete('/api/v1/oauth/connections/hubspot',headers=admin).json()['disconnected']
 assert api.get('/api/v1/oauth/connections',headers=admin).json()==[]

def test_exchange_rejects_malicious_remote_base(monkeypatch):
 import httpx
 configured(monkeypatch)
 original=httpx.Client
 def intercept(request):
  return httpx.Response(200,json={'access_token':'synthetic-access-token-123456',
   'refresh_token':'synthetic-refresh-token-123456',
   'instance_url':'https://attacker.invalid','expires_in':3600})
 monkeypatch.setattr(oauth.httpx,'Client',lambda **kw:original(transport=httpx.MockTransport(intercept),**kw))
 import pytest
 from fastapi import HTTPException
 with pytest.raises(HTTPException) as exc:
  oauth.exchange('salesforce','synthetic-client','synthetic-secret',
   'https://sandbox.revenue.example.invalid/api/v1/oauth/callback','fake-code','pkce')
 assert exc.value.status_code==502
