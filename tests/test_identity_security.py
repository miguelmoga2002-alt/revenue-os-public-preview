"""Identity-security regression tests on isolated SQLite fixtures.

Production concurrency guarantees additionally require PostgreSQL tests.
"""
import base64
import hashlib
from datetime import timedelta

import pytest
from cryptography.fernet import Fernet
from sqlalchemy import select, update

from app.auth import utcnow
from app.models import User, AdminTOTP, AuthLoginAttempt, Session as AuthSession
from app.security_auth import make_secret, encrypt_secret, decrypt_secret, verified_step, _totp


PASSWORD = "long-test-password-1234"


def _login(api, username="admin-a", password=PASSWORD, otp=None):
    body = {"username": username, "password": password}
    if otp is not None:
        body["otp"] = otp
    return api.post("/api/v1/auth/login", json=body)


def test_totp_rfc6238_vector_and_crypto(monkeypatch):
    # RFC 6238 SHA1 at T=59: 94287082 (8 digits), low six = 287082.
    secret = base64.b32encode(b"12345678901234567890").decode().rstrip("=")
    assert _totp(secret, 1) == "287082"
    assert verified_step(secret, "287082", timestamp=59) == 1
    assert verified_step(secret, "287082", last_step=1, timestamp=59) is None
    monkeypatch.setenv("REVENUE_OS_MFA_ENCRYPTION_KEY", Fernet.generate_key().decode())
    encrypted = encrypt_secret(secret)
    assert secret not in encrypted
    assert decrypt_secret(encrypted) == secret


def test_administrator_mfa_mandatory_replay_and_legacy_sessions(api, monkeypatch):
    previous = _login(api).json()["access_token"]
    monkeypatch.setenv("REVENUE_OS_REQUIRE_ADMIN_MFA", "1")
    assert api.get("/api/v1/me", headers={"Authorization": "Bearer " + previous}).status_code == 401
    assert _login(api).status_code == 403  # No self-enrollment from a password-only login.
    monkeypatch.setenv("REVENUE_OS_MFA_ENCRYPTION_KEY", Fernet.generate_key().decode())
    secret = make_secret()
    with api.app.state.test_session_factory.begin() as db:
        user = db.scalar(select(User).where(User.username == "admin-a"))
        db.add(AdminTOTP(tenant_id=user.tenant_id, user_id=user.id,
                         secret_ciphertext=encrypt_secret(secret), last_step=-1))
    assert _login(api, otp="000000").status_code == 401
    step = int(__import__("time").time()) // 30
    code = _totp(secret, step)
    response = _login(api, otp=code)
    assert response.status_code == 200, response.text
    new_token = response.json()["access_token"]
    assert api.get("/api/v1/me", headers={"Authorization": "Bearer " + new_token}).status_code == 200
    assert _login(api, otp=code).status_code == 401  # No reuse within the same timestep.
    assert api.get("/api/v1/me", headers={"Authorization": "Bearer " + previous}).status_code == 401


def test_persistent_account_throttling_and_isolation(api):
    wrong = {"username": "rep-a", "password": "not-the-right-password"}
    for i in range(5):
        result = api.post("/api/v1/auth/login", json=wrong)
        assert result.status_code == (429 if i == 4 else 401), result.text
    assert _login(api, "rep-a").status_code == 429
    with api.app.state.test_session_factory() as db:
        from app.models import Audit
        events = db.scalars(select(Audit).where(Audit.action == "login.failure")).all()
        assert len(events) == 5
        assert all("password" not in row.detail.lower() and "otp" not in row.detail.lower() for row in events)
    # A different tenant's account does not inherit another account's failures.
    assert _login(api, "rep-b").status_code == 200
    with api.app.state.test_session_factory.begin() as db:
        user = db.scalar(select(User).where(User.username == "rep-a"))
        attempt = db.get(AuthLoginAttempt, (user.tenant_id, user.id))
        assert attempt.failures == 5
        attempt.locked_until = utcnow() - timedelta(seconds=1)
        attempt.window_started_at = utcnow() - timedelta(minutes=20)
    assert _login(api, "rep-a").status_code == 200


def test_session_revocation_is_per_account_and_per_tenant(api):
    first = _login(api, "rep-a").json()["access_token"]
    second = _login(api, "rep-a").json()["access_token"]
    foreign = _login(api, "rep-b").json()["access_token"]
    h1 = {"Authorization": "Bearer " + first}
    h2 = {"Authorization": "Bearer " + second}
    hf = {"Authorization": "Bearer " + foreign}
    sessions = api.get("/api/v1/auth/sessions", headers=h1)
    assert sessions.status_code == 200
    own = sessions.json()
    assert len(own) == 2 and sum(r["current"] for r in own) == 1
    foreign_hash = hashlib.sha256(foreign.encode()).hexdigest()
    assert api.post("/api/v1/auth/sessions/" + foreign_hash + "/revoke", headers=h1).status_code == 404
    assert api.post("/api/v1/auth/sessions/revoke-others", headers=h1).json()["revoked"] == 1
    assert api.get("/api/v1/me", headers=h2).status_code == 401
    assert api.get("/api/v1/me", headers=h1).status_code == 200
    assert api.get("/api/v1/me", headers=hf).status_code == 200
    admin = _login(api).json()["access_token"]
    ha = {"Authorization": "Bearer " + admin}
    with api.app.state.test_session_factory() as db:
        rep_b = db.scalar(select(User).where(User.username == "rep-b"))
        rep_a = db.scalar(select(User).where(User.username == "rep-a"))
    assert api.post("/api/v1/admin/reps/" + rep_b.id + "/sessions/revoke",headers=ha).status_code == 404
    assert api.post("/api/v1/admin/reps/" + rep_a.id + "/sessions/revoke",headers=ha).json()["revoked"] >= 1
    assert api.get("/api/v1/me", headers=h1).status_code == 401
    assert api.get("/api/v1/me", headers=hf).status_code == 200


def test_current_logout_revokes_own_token_and_replay_returns_401(api):
    """Reusing the same bearer after a successful DELETE must fail server-side."""
    old = _login(api, "rep-a").json()["access_token"]
    other = _login(api, "rep-a").json()["access_token"]
    foreign = _login(api, "rep-b").json()["access_token"]
    old_auth = {"Authorization": "Bearer " + old}
    other_auth = {"Authorization": "Bearer " + other}
    foreign_auth = {"Authorization": "Bearer " + foreign}
    # The same bearer is valid before the logout.
    assert api.get("/api/v1/me", headers=old_auth).status_code == 200
    response = api.delete("/api/v1/auth/session", headers=old_auth)
    assert response.status_code == 200 and response.json() == {"revoked": True}
    # Uses a new request/database session (not just hidden frontend state).
    assert api.get("/api/v1/me", headers=old_auth).status_code == 401
    assert api.get("/api/v1/auth/sessions", headers=old_auth).status_code == 401
    assert api.delete("/api/v1/auth/session", headers=old_auth).status_code == 401
    assert api.get("/api/v1/me", headers=other_auth).status_code == 200
    assert api.get("/api/v1/me", headers=foreign_auth).status_code == 200
    digest = hashlib.sha256(old.encode()).hexdigest()
    with api.app.state.test_session_factory() as db:
        row = db.get(AuthSession, digest)
        assert row is not None and row.revoked is True
        from app.models import Audit
        user = db.scalar(select(User).where(User.username == "rep-a"))
        events = db.scalars(select(Audit).where(
            Audit.tenant_id == user.tenant_id,
            Audit.user_id == user.id, Audit.action == "logout")).all()
        assert len(events) == 1
        assert all(old not in row.detail for row in events)


def test_admin_with_enrolled_factor_requires_otp_even_if_override_off(api, monkeypatch):
    monkeypatch.setenv("REVENUE_OS_MFA_ENCRYPTION_KEY", Fernet.generate_key().decode())
    secret = make_secret()
    with api.app.state.test_session_factory.begin() as db:
        user = db.scalar(select(User).where(User.username == "admin-b"))
        db.add(AdminTOTP(tenant_id=user.tenant_id,user_id=user.id,
                         secret_ciphertext=encrypt_secret(secret),last_step=-1))
    assert _login(api, "admin-b").status_code == 401
