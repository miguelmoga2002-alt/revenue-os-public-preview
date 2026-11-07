"""Direct PostgreSQL RLS enforcement tests using a non-owner, non-BYPASSRLS role.

Requires PostgreSQL dedicated *_test database, Alembic 003 and provisioned
revenue_app NOLOGIN role with table DML grants (CI-only test role).
"""
import os
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session
from sqlalchemy.exc import DBAPIError

from app.models import CRMRecord, Tenant

URL = os.getenv("REVENUE_OS_TEST_POSTGRES_URL", "")


@pytest.mark.skipif(not URL, reason="Dedicated PostgreSQL test DB not configured")
def test_direct_sql_rls_isolation():
    url = make_url(URL)
    if not url.drivername.startswith("postgresql") or not url.database.endswith("_test"):
        pytest.fail("RLS test only permits PostgreSQL databases ending in _test")
    engine = create_engine(URL, pool_pre_ping=True)
    a, b = "ci-tenant-a-" + uuid4().hex[:12], "ci-tenant-b-" + uuid4().hex[:12]
    try:
        with engine.connect() as conn:
            tx = conn.begin()
            try:
                with Session(bind=conn) as db:
                    db.add_all([Tenant(id=a, name="Tenant A"), Tenant(id=b, name="Tenant B")])
                    db.flush()
                    db.add_all([
                        CRMRecord(id=str(uuid4()), tenant_id=a, kind="contacts",
                                  source="ci", external_id="same", name="A"),
                        CRMRecord(id=str(uuid4()), tenant_id=b, kind="contacts",
                                  source="ci", external_id="same", name="B"),
                    ])
                    db.flush()

                checks = conn.execute(text(
                    "SELECT relname FROM pg_class WHERE relname IN "
                    "('crm_records', 'work_items', 'import_batches', 'audit_events') "
                    "AND relrowsecurity"
                )).scalars().all()
                assert len(checks) == 4, "RLS migration was not applied"

                # Explicitly avoid superuser/table-owner RLS bypass.
                conn.exec_driver_sql("SET LOCAL ROLE revenue_app")
                assert conn.scalar(text("SELECT current_user")) == "revenue_app"
                assert not conn.scalar(text(
                    "SELECT rolbypassrls FROM pg_roles WHERE rolname=current_user"
                ))

                # Unset setting must default to no rows.
                assert conn.scalar(text("SELECT count(*) FROM crm_records")) == 0
                conn.execute(text(
                    "SELECT set_config('revenue_os.tenant_id', :tenant_id, true)"
                ), {"tenant_id": a})
                result = conn.execute(text(
                    "SELECT name FROM crm_records ORDER BY name"
                )).scalars().all()
                assert result == ["A"], result

                # WITH CHECK must prevent a row targeted at another tenant.
                sp = conn.begin_nested()
                try:
                    with pytest.raises(DBAPIError):
                        conn.execute(text(
                            "INSERT INTO crm_records "
                            "(id, tenant_id, kind, source, external_id, name, attributes, created_at, updated_at) "
                            "VALUES (:id, :tenant, 'contacts', 'ci', 'forged', 'Forged', '{}', now(), now())"
                        ), {"id": str(uuid4()), "tenant": b})
                finally:
                    sp.rollback()

                conn.execute(text(
                    "SELECT set_config('revenue_os.tenant_id', :tenant_id, true)"
                ), {"tenant_id": b})
                assert conn.execute(text(
                    "SELECT name FROM crm_records ORDER BY name"
                )).scalars().all() == ["B"]

                conn.execute(text(
                    "SELECT set_config('revenue_os.tenant_id', '', true)"
                ))
                assert conn.scalar(text("SELECT count(*) FROM crm_records")) == 0
            finally:
                tx.rollback()
    finally:
        engine.dispose()
