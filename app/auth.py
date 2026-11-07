"""Authentication primitives, per-account lockout and session principal.

Authenticated principals must derive tenant identity from persisted users,
never from request headers or client-supplied tenant IDs.
"""
import hashlib
import hmac
import secrets
import uuid
from datetime import datetime, timedelta, timezone

from fastapi import Depends, Header, HTTPException
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session as DBSession

from .db import get_db
from .tenant_context import set_tenant_context
from .models import User, Session, Audit, AuthLoginAttempt, AdminTOTP, SessionProof
from .security_auth import require_admin_mfa


def uid():
    return str(uuid.uuid4())


def utcnow():
    return datetime.now(timezone.utc)


def aware(value):
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def hash_password(password):
    salt = secrets.token_bytes(16)
    return f'{salt.hex()}:{hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 390000).hex()}'


def verify(password, stored):
    try:
        salt, digest = stored.split(":")
        return hmac.compare_digest(
            hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), 390000),
            bytes.fromhex(digest))
    except (ValueError, AttributeError, UnicodeError):
        return False


# Deterministic dummy work factor prevents a conspicuously cheap unknown-user path.
DUMMY_HASH = "00" * 16 + ":" + "00" * 32
MAX_ATTEMPTS = 5
WINDOW = timedelta(minutes=15)
LOCKOUT = timedelta(minutes=15)


def login_attempt_for(db: DBSession, user: User) -> AuthLoginAttempt:
    values = dict(tenant_id=user.tenant_id, user_id=user.id, failures=0,
                  window_started_at=utcnow())
    dialect = db.get_bind().dialect.name
    if dialect == "postgresql":
        stmt = pg_insert(AuthLoginAttempt).values(**values).on_conflict_do_nothing(
            index_elements=["tenant_id", "user_id"])
    elif dialect == "sqlite":
        stmt = sqlite_insert(AuthLoginAttempt).values(**values).on_conflict_do_nothing(
            index_elements=["tenant_id", "user_id"])
    else:
        raise HTTPException(503, "Motor de autenticación no compatible")
    db.execute(stmt)
    attempt = db.scalar(select(AuthLoginAttempt).where(
        AuthLoginAttempt.tenant_id == user.tenant_id,
        AuthLoginAttempt.user_id == user.id).with_for_update())
    if attempt is None:
        raise HTTPException(503, "Autenticación no disponible")
    now = utcnow()
    if attempt.locked_until and aware(attempt.locked_until) > now:
        raise HTTPException(429, "Demasiados intentos; inténtalo más tarde",
                            headers={"Retry-After": "900"})
    if aware(attempt.window_started_at) + WINDOW <= now:
        attempt.failures = 0
        attempt.window_started_at = now
        attempt.locked_until = None
    return attempt


def deny_login(db: DBSession, attempt: AuthLoginAttempt, user: User) -> None:
    attempt.failures += 1
    if attempt.failures >= MAX_ATTEMPTS:
        attempt.locked_until = utcnow() + LOCKOUT
    blocked = attempt.failures >= MAX_ATTEMPTS
    # Never store password, OTP, raw token or request payload in security audit.
    record_audit(db, user, "login.failure", user.id,
                 {"failures": attempt.failures, "locked": blocked})
    db.commit()  # persist failures across workers even when authorization fails
    if blocked:
        raise HTTPException(429, "Demasiados intentos; inténtalo más tarde",
                            headers={"Retry-After": "900"})
    raise HTTPException(401, "Credenciales inválidas")


def principal(authorization: str = Header(default=""), db: DBSession = Depends(get_db)):
    if not authorization.startswith("Bearer "):
        raise HTTPException(401, "Se requiere sesión")
    token = authorization[7:]
    if len(token) != 64:
        raise HTTPException(401, "Sesión inválida")
    token_hash = hashlib.sha256(token.encode()).hexdigest()
    row = db.get(Session, token_hash)
    if not row or row.revoked:
        raise HTTPException(401, "Sesión no válida")
    if aware(row.expires_at) <= utcnow():
        raise HTTPException(401, "Sesión caducada")
    user = db.get(User, row.user_id)
    if not user or not user.active:
        raise HTTPException(401, "Usuario inactivo")
    set_tenant_context(db, user.tenant_id)
    if user.role == "admin":
        enrolled = db.get(AdminTOTP, (user.tenant_id, user.id))
        if require_admin_mfa() or enrolled:
            proof = db.get(SessionProof, token_hash)
            if not proof or proof.tenant_id != user.tenant_id or not proof.mfa_verified:
                raise HTTPException(401, "Se requiere una sesión administrativa con MFA")
    return user


def admin(user: User = Depends(principal)):
    if user.role != "admin":
        raise HTTPException(403, "Solo administración")
    return user


def record_audit(db, user, action, target, detail):
    import json
    db.add(Audit(id=uid(), tenant_id=user.tenant_id, user_id=user.id,
                 action=action, target=target, detail=json.dumps(detail, ensure_ascii=False)))
