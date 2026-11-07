"""TOTP RFC 6238 verification and encrypted at-rest credentials.

A secret is provisioned out-of-band by a trusted operator. There is no
password-only endpoint for installing/recovering an administrator factor.
"""
import base64
import hashlib
import hmac
import os
import secrets
import struct
import time
from urllib.parse import quote

from cryptography.fernet import Fernet, InvalidToken
from fastapi import HTTPException


def require_admin_mfa() -> bool:
    # Fail closed by default. Only isolated development/legacy CI may opt out.
    return os.getenv("REVENUE_OS_REQUIRE_ADMIN_MFA", "1") != "0"


def _fernet() -> Fernet:
    key = os.getenv("REVENUE_OS_MFA_ENCRYPTION_KEY", "")
    try:
        return Fernet(key.encode("ascii"))
    except (ValueError, TypeError) as exc:
        raise HTTPException(503, "MFA no disponible; configuración incompleta") from exc


def make_secret() -> str:
    return base64.b32encode(secrets.token_bytes(20)).decode("ascii").rstrip("=")


def encrypt_secret(secret: str) -> str:
    return _fernet().encrypt(secret.encode("ascii")).decode("ascii")


def decrypt_secret(encrypted: str) -> str:
    try:
        return _fernet().decrypt(encrypted.encode("ascii")).decode("ascii")
    except (InvalidToken, ValueError, UnicodeError) as exc:
        raise HTTPException(503, "MFA no disponible; contactar administración") from exc


def _totp(secret: str, counter: int) -> str:
    raw = base64.b32decode(secret + ("=" * (-len(secret) % 8)), casefold=False)
    digest = hmac.new(raw, struct.pack(">Q", counter), hashlib.sha1).digest()
    offset = digest[-1] & 15
    digits = (struct.unpack(">I", digest[offset:offset + 4])[0] & 0x7fffffff) % 1000000
    return f"{digits:06d}"


def verified_step(secret: str, code: str | None, last_step: int = -1,
                  timestamp: float | None = None) -> int | None:
    if code is None or len(code) != 6 or not code.isascii() or not code.isdecimal():
        return None
    now_step = int(time.time() if timestamp is None else timestamp) // 30
    # Allow modest clock skew but reject a code accepted in this or a prior window.
    for step in (now_step, now_step - 1, now_step + 1):
        if step > last_step and hmac.compare_digest(_totp(secret, step), code):
            return step
    return None


def provisioning_uri(secret: str, username: str) -> str:
    label = quote("Revenue OS:" + username, safe="")
    return f"otpauth://totp/{label}?secret={secret}&issuer=Revenue%20OS&algorithm=SHA1&digits=6&period=30"
