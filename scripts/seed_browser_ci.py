"""Synthetic, disposable browser E2E identities. Never run on customer databases.

Only the dedicated GitHub Actions Compose job can enable this script.
It does not create migrations or modify any existing users.
"""
import os

from sqlalchemy import select, text

from app.auth import hash_password, uid
from app.db import SessionLocal, engine
from app.models import Tenant, User

EXPECTED_DB = "revenue_os"
PREFIX = "ci-browser-v11"
PASSWORD = "E2E_only_synthetic_2026_not_a_real_secret!"


def main() -> None:
    if (os.getenv("GITHUB_ACTIONS") != "true"
            or os.getenv("REVENUE_OS_E2E_SEED") != "synthetic-ci-only"):
        raise SystemExit("Refusing to seed outside isolated GitHub Actions browser CI")
    with engine.connect() as conn:
        dbname, role = conn.execute(text("SELECT current_database(), current_user")).one()
        if dbname != EXPECTED_DB or role != "revenue_app":
            raise SystemExit("Requires isolated Compose DB and restricted runtime account")
        version = conn.scalar(text("SELECT version_num FROM alembic_version"))
        if version != "012_deal_notes":
            raise SystemExit("Migrations must be at v1.1 head before E2E seed")

    with SessionLocal.begin() as db:
        names = ("e2e-admin-a", "e2e-rep-a", "e2e-admin-b", "e2e-rep-b")
        if db.scalar(select(User.id).where(User.username.in_(names))):
            raise SystemExit("Test identities already exist; refusing to modify them")
        db.add_all([
            Tenant(id=PREFIX+"-a", name="CI Synthetic A"),
            Tenant(id=PREFIX+"-b", name="CI Synthetic B"),
        ])
        db.flush()
        for tenant, suffix in (("a", "-a"), ("b", "-b")):
            for role, user in (("admin", "e2e-admin"), ("rep", "e2e-rep")):
                db.add(User(
                    id=uid(), tenant_id=PREFIX+suffix,
                    username=user+suffix, role=role,
                    password_hash=hash_password(PASSWORD), active=True
                ))
    print("Seeded synthetic E2E tenants and roles (CI-only).")


if __name__ == "__main__":
    main()
