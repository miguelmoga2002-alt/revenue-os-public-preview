"""Restricted-role concurrent login lockout and direct cross-tenant RLS checks."""
import os
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from uuid import uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session

from app.auth import hash_password
from app.main import Login, login
from app.models import Tenant, User, AdminTOTP, AuthLoginAttempt
from app.tenant_context import set_tenant_context

URL = os.getenv("REVENUE_OS_TEST_POSTGRES_URL", "")
PASS = os.getenv("REVENUE_OS_TEST_APP_PASSWORD", "")


@pytest.mark.skipif(not URL or not PASS, reason="Dedicated PostgreSQL test DB required")
def test_two_workers_share_lockout_and_tenant_rls():
    url = make_url(URL)
    if not url.drivername.startswith("postgresql") or not url.database.endswith("_test"):
        pytest.fail("Refuse to run against non-test database")
    owner = create_engine(URL, pool_pre_ping=True)
    runtime = create_engine(url.set(username="revenue_app", password=PASS), pool_pre_ping=True)
    suffix = uuid4().hex[:12]
    ta, tb = "ci-auth-a-" + suffix, "ci-auth-b-" + suffix
    ua, ub = str(uuid4()), str(uuid4())
    try:
        with Session(owner) as db:
            db.add_all([Tenant(id=ta, name="Auth A"), Tenant(id=tb, name="Auth B")])
            db.flush()
            db.add_all([
                User(id=ua, tenant_id=ta, username="auth-a-" + suffix,role="rep",
                     password_hash=hash_password("long-correct-test-password")),
                User(id=ub, tenant_id=tb, username="auth-b-" + suffix,role="rep",
                     password_hash=hash_password("long-correct-test-password")),
            ])
            db.commit()
        barrier = Barrier(2)
        def attempt(_):
            with Session(runtime, expire_on_commit=False) as db:
                barrier.wait(timeout=15)
                try:
                    login(Login(username="auth-a-" + suffix,password="wrong-password"),db=db)
                except HTTPException as err:
                    return err.status_code
                return 200
        with ThreadPoolExecutor(max_workers=2) as executor:
            outcomes = list(executor.map(attempt, range(2)))
        assert outcomes == [401,401], outcomes
        with Session(owner) as db:
            assert db.scalar(text("SELECT failures FROM auth_login_attempts "
                                  "WHERE tenant_id=:tenant AND user_id=:uid"),
                             {"tenant": ta, "uid": ua}) == 2
            assert db.scalar(text("SELECT COUNT(*) FROM audit_events WHERE tenant_id=:t "
                                  "AND action='login.failure'"),{"t":ta}) == 2
            assert db.scalar(text("SELECT count(*) FROM session_proofs WHERE tenant_id=:t"),
                             {"t": ta}) == 0
        with Session(runtime) as db:
            set_tenant_context(db,ta)
            assert db.scalar(select(AuthLoginAttempt).where(
                AuthLoginAttempt.tenant_id==tb)) is None
            # Even direct SQL writes to another tenant are rejected by RLS.
            assert db.execute(text("UPDATE auth_login_attempts SET failures=99 "
                                   "WHERE tenant_id=:t"), {"t":tb}).rowcount == 0
            assert db.scalar(select(AdminTOTP).where(AdminTOTP.tenant_id==tb)) is None
    finally:
        with owner.begin() as c:
            c.execute(text("DELETE FROM audit_events WHERE tenant_id IN (:a,:b)"),{"a":ta,"b":tb})
            c.execute(text("DELETE FROM session_proofs WHERE tenant_id IN (:a,:b)"),{"a":ta,"b":tb})
            c.execute(text("DELETE FROM auth_login_attempts WHERE tenant_id IN (:a,:b)"),{"a":ta,"b":tb})
            c.execute(text("DELETE FROM admin_totp WHERE tenant_id IN (:a,:b)"),{"a":ta,"b":tb})
            c.execute(text("DELETE FROM users WHERE tenant_id IN (:a,:b)"),{"a":ta,"b":tb})
            c.execute(text("DELETE FROM tenants WHERE id IN (:a,:b)"),{"a":ta,"b":tb})
        owner.dispose()
        runtime.dispose()
