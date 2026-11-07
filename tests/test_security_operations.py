"""Production configuration validation and tenant security-alert regression."""
from datetime import timedelta
from cryptography.fernet import Fernet
from sqlalchemy import select
from app.runtime_security import production_blockers,require_secure_runtime
from app.models import Audit
from app.auth import uid,utcnow
from .conftest import login

def safe_production():
 return {
  'REVENUE_OS_ENV':'production',
  'REVENUE_OS_REQUIRE_ADMIN_MFA':'1',
  'REVENUE_OS_PUBLIC_ORIGIN':'https://revenue.example.invalid',
  'REVENUE_OS_DATABASE_URL':'postgresql+psycopg://revenue_app:synthetic-only@db:5432/revenue_os',
  'REVENUE_OS_MFA_ENCRYPTION_KEY':Fernet.generate_key().decode(),
  'REVENUE_OS_ENABLE_PIPEDRIVE_SYNC':'0',
  'REVENUE_OS_ENABLE_CRM_OAUTH':'0'
 }

def test_production_configuration_fails_closed_without_leaking_credentials():
 env=safe_production()
 assert production_blockers(env)==[]
 fail={**env,'REVENUE_OS_REQUIRE_ADMIN_MFA':'0',
        'REVENUE_OS_PUBLIC_ORIGIN':'http://revenue.example.invalid',
        'REVENUE_OS_DATABASE_URL':'postgresql+psycopg://revenue:supersecret@db/test',
        'REVENUE_OS_MFA_ENCRYPTION_KEY':'bad-key',
        'REVENUE_OS_ENABLE_PIPEDRIVE_SYNC':'1'}
 issues=production_blockers(fail)
 assert {'admin_mfa_disabled','public_https_origin_required',
         'restricted_database_role_required','mfa_encryption_key_required',
         'legacy_pipedrive_api_key_not_approved_for_production'}<=set(issues)
 assert 'supersecret' not in repr(issues)
 assert production_blockers({'REVENUE_OS_ENV':'development'})==[]

def test_oauth_keys_must_be_distinct_and_callback_exact():
 env=safe_production()
 env['REVENUE_OS_ENABLE_CRM_OAUTH']='1'
 env['REVENUE_OS_OAUTH_ENCRYPTION_KEY']=env['REVENUE_OS_MFA_ENCRYPTION_KEY']
 env['REVENUE_OS_OAUTH_CALLBACK_URL']='https://other.example.invalid/api/v1/oauth/callback'
 issues=production_blockers(env)
 assert 'distinct_encryption_keys_required' in issues
 assert 'oauth_callback_must_match_public_origin' in issues
 env['REVENUE_OS_OAUTH_ENCRYPTION_KEY']=Fernet.generate_key().decode()
 env['REVENUE_OS_OAUTH_CALLBACK_URL']=env['REVENUE_OS_PUBLIC_ORIGIN']+'/api/v1/oauth/callback'
 assert production_blockers(env)==[]

def test_runtime_rejects_insecure_production_environment(monkeypatch):
 env=safe_production()
 for key,value in env.items():monkeypatch.setenv(key,value)
 require_secure_runtime()
 monkeypatch.setenv('REVENUE_OS_REQUIRE_ADMIN_MFA','0')
 import pytest
 with pytest.raises(RuntimeError,match='admin_mfa_disabled'):
  require_secure_runtime()

def test_alerts_are_admin_only_and_tenant_scoped(api):
 admin=login(api,'admin-a')
 other=login(api,'admin-b')
 rep=login(api,'rep-a')
 assert api.get('/api/v1/security/alerts').status_code==401
 assert api.get('/api/v1/security/alerts',headers=rep).status_code==403
 with api.app.state.test_session_factory.begin() as db:
  from app.models import User
  for tenant in ('a','b'):
   user=db.scalar(select(User).where(User.tenant_id==tenant,User.username=='admin-'+tenant))
   for i in range(4):
    db.add(Audit(id=uid(),tenant_id=tenant,user_id=user.id,action='login.failure',
      target=user.id,detail='{}',created_at=utcnow()))
   db.add(Audit(id=uid(),tenant_id=tenant,user_id=user.id,action='crm.oauth.disconnect',
     target='hubspot',detail='{}',created_at=utcnow()))
 result=api.get('/api/v1/security/alerts',headers=admin)
 assert result.status_code==200,result.text
 body=result.json()
 assert body['delivery']=='admin_pull_only' and body['truncated'] is False
 assert len([x for x in body['findings'] if x['code']=='repeated_login_failures'])==1
 assert any(x.get('event_type')=='crm.oauth.disconnect' for x in body['findings'])
 assert body['events_examined']==5
 foreign=api.get('/api/v1/security/alerts',headers=other).json()
 assert foreign['events_examined']==5
 assert foreign['findings'][0]['actor_id']!=body['findings'][0]['actor_id']
