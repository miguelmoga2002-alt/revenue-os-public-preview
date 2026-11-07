"""Ensure composite foreign keys reject cross-tenant CRM links at SQL level.

The application can never link records from A to B using a forged related_id,
even if the importing code contains a programming error.
"""
import os
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import IntegrityError

URL = os.environ.get("REVENUE_OS_TEST_POSTGRES_URL", "")


@pytest.mark.skipif(not URL, reason="PostgreSQL test URL required")
def test_crm_composite_fk_blocks_cross_tenant_reference():
    parsed = make_url(URL)
    if not parsed.drivername.startswith("postgresql") or not parsed.database.endswith("_test"):
        pytest.fail("Dedicated PostgreSQL *_test database required")
    engine = create_engine(URL)
    a, b = str(uuid4()), str(uuid4())
    ra, rb = str(uuid4()), str(uuid4())
    try:
        with engine.connect() as conn:
            tx = conn.begin()
            try:
                conn.execute(text("INSERT INTO tenants(id,name) VALUES (:a,'A'),(:b,'B')"), {"a": a, "b": b})
                for tenant, record in ((a, ra), (b, rb)):
                    conn.execute(text(
                        "INSERT INTO crm_records (id,tenant_id,kind,source,external_id,name,attributes,created_at,updated_at)"
                        " VALUES (:r,:t,'companies','ci',:e,'Example','{}',now(),now())"
                    ), {"r": record, "t": tenant, "e": record})
                # Force same-tenant link succeeds.
                conn.execute(text(
                    "INSERT INTO crm_links (tenant_id,record_id,relation,related_id)"
                    " VALUES (:t,:r,'company',:r)"
                ), {"t": a, "r": ra})
                # A source record must never point to B, even when the caller
                # is the DB owner and RLS is bypassed (integrity is DB-bound).
                sp = conn.begin_nested()
                try:
                    with pytest.raises(IntegrityError):
                        conn.execute(text(
                            "UPDATE crm_links SET related_id=:foreign WHERE tenant_id=:t"
                        ), {"foreign": rb, "t": a})
                finally:
                    sp.rollback()
            finally:
                tx.rollback()
    finally:
        engine.dispose()
