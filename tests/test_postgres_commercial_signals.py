"""PostgreSQL commercial_signals security test under restricted runtime role."""
import os
from uuid import uuid4
import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session
from app.models import Tenant, User, CRMRecord, CommercialSignal
from app.auth import hash_password, utcnow
from app.tenant_context import set_tenant_context

URL=os.getenv('REVENUE_OS_TEST_POSTGRES_URL','')
PASSWORD=os.getenv('REVENUE_OS_TEST_APP_PASSWORD','')

@pytest.mark.skipif(not URL or not PASSWORD,reason='Dedicated PostgreSQL CI database only')
def test_signals_rls_restricted_actor_and_cross_tenant_isolation():
 url=make_url(URL)
 if not url.drivername.startswith('postgresql') or not url.database.endswith('_test'):
  pytest.fail('Only an isolated PostgreSQL _test database is allowed')
 owner=create_engine(URL,pool_pre_ping=True)
 runtime=create_engine(url.set(username='revenue_app',password=PASSWORD),pool_pre_ping=True)
 suffix=uuid4().hex[:12];tenants=['sig-a-'+suffix,'sig-b-'+suffix]
 try:
  with Session(owner) as db:
   for tenant in tenants:
    db.add(Tenant(id=tenant,name=tenant))
    db.flush()
    user=User(id=str(uuid4()),tenant_id=tenant,username='sig-'+tenant,
              role='rep',password_hash=hash_password('synthetic-pass-not-a-secret'))
    db.add(user)
    record=CRMRecord(id=str(uuid4()),tenant_id=tenant,kind='contacts',source='ci',
                     external_id='same',name=tenant)
    db.add(record)
    db.flush()
    db.add(CommercialSignal(id=str(uuid4()),tenant_id=tenant,record_id=record.id,
      actor_id=user.id,kind='no_answer',source_type='work_result',
      source_ref='synthetic-'+tenant,occurred_at=utcnow(),
      verification='recorded_action',status='current',detail='synthetic'))
   db.commit()
  with Session(runtime) as db:
   assert db.scalar(text("SELECT relrowsecurity FROM pg_class WHERE relname='commercial_signals'")) is True
   assert db.scalar(text("SELECT count(*) FROM commercial_signals"))==0
   set_tenant_context(db,tenants[0])
   rows=db.scalars(__import__('sqlalchemy').select(CommercialSignal)).all()
   assert len(rows)==1 and rows[0].tenant_id==tenants[0]
   updated=db.execute(text("UPDATE commercial_signals SET kind='replied' WHERE tenant_id=:tenant"),
                      {'tenant':tenants[1]})
   assert updated.rowcount==0
   db.rollback()
  with Session(runtime) as db:
   set_tenant_context(db,tenants[1])
   assert [row.tenant_id for row in db.scalars(__import__('sqlalchemy').select(CommercialSignal)).all()]==[tenants[1]]
 finally:
  with owner.begin() as conn:
   for table in ('commercial_signals','crm_records','users','tenants'):
    conn.execute(text(f"DELETE FROM {table} WHERE "+('id' if table=='tenants' else 'tenant_id')+" IN (:a,:b)"),
                 {'a':tenants[0],'b':tenants[1]})
  owner.dispose();runtime.dispose()
