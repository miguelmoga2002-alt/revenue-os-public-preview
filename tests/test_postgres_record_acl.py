"""Direct composite FK ACL check: cannot grant rep B access to record A."""
import os
from uuid import uuid4
import pytest
from sqlalchemy import create_engine,text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import IntegrityError

URL=os.getenv("REVENUE_OS_TEST_POSTGRES_URL","")

@pytest.mark.skipif(not URL,reason="Postgres CI test only")
def test_acl_composite_fk_rejects_cross_tenant_grant():
    u=make_url(URL)
    if not u.drivername.startswith("postgresql") or not u.database.endswith("_test"):
        pytest.fail("Dedicated *_test database required")
    engine=create_engine(URL)
    t1,t2,u2,r1=(str(uuid4()) for _ in range(4))
    try:
        with engine.connect() as conn:
            tx=conn.begin()
            try:
                conn.execute(text("INSERT INTO tenants(id,name) VALUES (:a,'A'),(:b,'B')"),{"a":t1,"b":t2})
                conn.execute(text("INSERT INTO users(id,tenant_id,username,password_hash,role,active) VALUES (:u,:t,:n,'test','rep',true)"),
                             {"u":u2,"t":t2,"n":"rep-"+u2})
                conn.execute(text("INSERT INTO crm_records(id,tenant_id,kind,source,external_id,name,attributes,created_at,updated_at) VALUES (:r,:t,'contacts','test',:e,'Lead','{}',now(),now())"),
                             {"r":r1,"t":t1,"e":r1})
                sp=conn.begin_nested()
                try:
                    with pytest.raises(IntegrityError):
                        conn.execute(text("INSERT INTO record_grants(tenant_id,user_id,record_id) VALUES (:t,:u,:r)"),
                                     {"t":t1,"u":u2,"r":r1})
                finally:
                    sp.rollback()
            finally:
                tx.rollback()
    finally:
        engine.dispose()
