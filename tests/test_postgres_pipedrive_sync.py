"""PostgreSQL RLS for Pipedrive incremental sync checkpoints with restricted runtime role."""
import os
from datetime import datetime, timezone
from uuid import uuid4
import pytest
from sqlalchemy import create_engine, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session
from app.models import Tenant, CRMSyncCheckpoint
from app.tenant_context import set_tenant_context

URL=os.getenv("REVENUE_OS_TEST_POSTGRES_URL","")
PASSWORD=os.getenv("REVENUE_OS_TEST_APP_PASSWORD","")
@pytest.mark.skipif(not URL or not PASSWORD,reason="Dedicated test database required")
def test_crm_checkpoint_rls_direct_sql():
 url=make_url(URL)
 if not url.drivername.startswith("postgresql") or not url.database.endswith("_test"):
  pytest.fail("Only dedicated *_test PostgreSQL database allowed")
 owner=create_engine(URL,pool_pre_ping=True)
 runtime=create_engine(url.set(username="revenue_app",password=PASSWORD),pool_pre_ping=True)
 a,b="ci-pd-a-"+uuid4().hex[:12],"ci-pd-b-"+uuid4().hex[:12]
 try:
  with Session(owner) as db:
   db.add_all([Tenant(id=a,name="Sync A"),Tenant(id=b,name="Sync B")]);db.flush()
   for tenant in (a,b):
    db.add(CRMSyncCheckpoint(tenant_id=tenant,provider="pipedrive",kind="companies",
      last_completed_until=datetime.now(timezone.utc),imported_count=1))
   db.commit()
  with Session(runtime) as db:
   assert db.scalar(text("SELECT relrowsecurity FROM pg_class WHERE relname='crm_sync_checkpoints'")) is True
   assert db.scalar(text("SELECT COUNT(*) FROM crm_sync_checkpoints"))==0
   set_tenant_context(db,a)
   rows=db.scalars(select(CRMSyncCheckpoint)).all()
   assert len(rows)==1 and rows[0].tenant_id==a
   assert db.execute(text("UPDATE crm_sync_checkpoints SET imported_count=999 "
     "WHERE tenant_id=:tenant"),{"tenant":b}).rowcount==0
  with Session(runtime) as db:
   set_tenant_context(db,b)
   assert len(db.scalars(select(CRMSyncCheckpoint)).all())==1
 finally:
  with owner.begin() as c:
   c.execute(text("DELETE FROM crm_sync_checkpoints WHERE tenant_id IN (:a,:b)"),{"a":a,"b":b})
   c.execute(text("DELETE FROM tenants WHERE id IN (:a,:b)"),{"a":a,"b":b})
  owner.dispose();runtime.dispose()
