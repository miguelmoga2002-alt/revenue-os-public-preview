"""One-time privileged TOTP enrollment. Run locally via isolated migration role.

Never run this command in GitHub Actions, and never paste URI/secret into chat,
issues, CI logs or shell history. The operator must independently authenticate
the administrator before enrolling or recovering the second factor.
"""
import getpass
import os
import sys
from sqlalchemy import select
from app.db import SessionLocal, DATABASE_URL
from app.models import User, AdminTOTP, Session
from app.security_auth import make_secret, provisioning_uri, verified_step, encrypt_secret


def main():
    if len(sys.argv) != 2 or not DATABASE_URL.startswith("postgresql"):
        raise SystemExit("Usage: python -m scripts.enroll_admin_mfa <admin-username> (PostgreSQL owner DB only)")
    if not os.getenv("REVENUE_OS_MFA_ENCRYPTION_KEY"):
        raise SystemExit("Set private REVENUE_OS_MFA_ENCRYPTION_KEY in operator environment")
    username = sys.argv[1]
    with SessionLocal.begin() as db:
        user = db.scalar(select(User).where(User.username == username, User.role == "admin",
                                            User.active == True))
        if not user:
            raise SystemExit("Active administrator not found")
        if db.get(AdminTOTP, (user.tenant_id, user.id)):
            raise SystemExit("Already enrolled; recovery requires a separate authorized process")
        secret = make_secret()
        print("Open this URI only on the trusted operator terminal; do NOT log or share it:")
        print(provisioning_uri(secret, user.username))
        code = getpass.getpass("Enter a current six-digit code from the authenticator: ")
        step = verified_step(secret, code)
        if step is None:
            raise SystemExit("Incorrect code; enrollment cancelled")
        db.add(AdminTOTP(tenant_id=user.tenant_id,user_id=user.id,
                         secret_ciphertext=encrypt_secret(secret),last_step=step))
        # All existing admin bearer tokens are invalidated on factor enrollment.
        for row in db.scalars(select(Session).where(Session.user_id == user.id)):
            row.revoked = True
    print("MFA enrollment complete; prior sessions revoked. Use a NEW OTP for login.")


if __name__ == "__main__":
    main()
