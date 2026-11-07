"""PostgreSQL RLS for OAuth state and encrypted CRM integrations."""
import os
from datetime import datetime,timedelta,timezone
from uuid import uuid4
import pytest
from sqlalchemy import create_engine,select,text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session
from app.models import Tenant,User,CRMOAuthPending,CRMOAuthConnection
from app.auth import hash_password
from app.tenant_context import set_tenant_context

URL=os.getenv('REVENUE_OS_TEST_POSTGRES_URL','')
PASSWORD=os.getenv('REVENUE_OS_TEST_APP_PASSWORD','')

@pytest.mark.skipif(not URL or not PASSWORD,reason='Dedicated PostgreSQL CI test database only')
def test_oauth_credentials_fail_closed_and_tenant_isolated():
 url=make_url(URL)
 if not url.drivername.startswith('postgresql') or not url.database.endswith('_test'):
  pytest.fail('Refuse non-test PostgreSQL database')
 owner=create_engine(URL,pool_pre_ping=True)
 runtime=create_engine(url.set(username='revenue_app',password=PASSWORD),pool_pre_ping=True)
 suffix=uuid4().hex[:12];ta,tb='oauth-a-'+suffix,'oauth-b-'+suffix
 try:
  with Session(owner) as db:
   for tenant in (ta,tb):
    db.add(Tenant(id=tenant,name=tenant))
    db.flush()
    actor=str(uuid4())
    db.add(User(id=actor,tenant_id=tenant,username='oauth-'+tenant,role='admin',
                password_hash=hash_password('synthetic-password')))
    db.flush()
    db.add(CRMOAuthPending(id=str(uuid4()),tenant_id=tenant,actor_id=actor,
       provider='hubspot',state_hash=uuid4().hex,verifier_ciphertext='ciphertext-only',
       expires_at=datetime.now(timezone.utc)+timedelta(minutes=5)))
    db.add(CRMOAuthConnection(tenant_id=tenant,provider='hubspot',
       account_key='synthetic',token_ciphertext='ciphertext-only',
       api_base='https://api.hubapi.com',scopes='crm.objects.contacts.read'))
   db.commit()
  with Session(runtime) as db:
   for table in ('crm_oauth_pending','crm_oauth_connections'):
    assert db.scalar(text('SELECT relrowsecurity FROM pg_class WHERE relname=:table'),{'table':table}) is True
    assert db.scalar(text('SELECT count(*) FROM '+table))==0
   set_tenant_context(db,ta)
   assert len(db.scalars(select(CRMOAuthPending)).all())==1
   assert len(db.scalars(select(CRMOAuthConnection)).all())==1
   assert db.execute(text('UPDATE crm_oauth_connections SET scopes=:scope WHERE tenant_id=:tenant'),
     {'scope':'forged','tenant':tb}).rowcount==0
  with Session(runtime) as db:
   set_tenant_context(db,tb)
   assert len(db.scalars(select(CRMOAuthConnection)).all())==1
 finally:
  with owner.begin() as c:
   for table in ('crm_oauth_pending','crm_oauth_connections','users','tenants'):
    column='id' if table=='tenants' else 'tenant_id'
    c.execute(text('DELETE FROM '+table+' WHERE '+column+' IN (:a,:b)'),{'a':ta,'b':tb})
  owner.dispose();runtime.dispose()
