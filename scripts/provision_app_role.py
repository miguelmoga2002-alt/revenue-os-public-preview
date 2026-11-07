"""Provision a least-privilege runtime database login after Alembic migrations.

Run only from an isolated migration container using the database owner URL.
Never supply the owner URL to the long-running API container.
"""
import os
from sqlalchemy import create_engine, text
from psycopg import sql

ADMIN_URL = os.environ.get("REVENUE_OS_DATABASE_URL", "")
PASSWORD = os.environ.get("REVENUE_OS_APP_DB_PASSWORD", "")


def main() -> None:
    if not ADMIN_URL.startswith("postgresql+") or len(PASSWORD) < 20:
        raise SystemExit("PostgreSQL owner URL and app password (20+ chars) are required")
    if PASSWORD.startswith("CHANGE_THIS_"):
        raise SystemExit("Replace the example app password")
    engine = create_engine(ADMIN_URL, pool_pre_ping=True)
    try:
        with engine.begin() as conn:
            owner = conn.scalar(text("SELECT current_user"))
            role = conn.scalar(text("SELECT rolname FROM pg_roles WHERE rolname='revenue_app'"))
            if role is None:
                conn.execute(text("CREATE ROLE revenue_app LOGIN NOSUPERUSER NOBYPASSRLS"))
            # PostgreSQL role DDL does not accept bind parameters for PASSWORD.
            # psycopg.sql.Literal performs correct SQL quoting (no string concatenation).
            with conn.connection.driver_connection.cursor() as cur:
                cur.execute(sql.SQL("ALTER ROLE revenue_app LOGIN NOSUPERUSER NOBYPASSRLS PASSWORD {}").format(sql.Literal(PASSWORD)))
            conn.execute(text("GRANT USAGE ON SCHEMA public TO revenue_app"))
            conn.execute(text("GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO revenue_app"))
            # Include future tables added by the same Alembic migration owner.
            conn.execute(text("ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO revenue_app"))
        print("Runtime role revenue_app provisioned (non-owner, NO BYPASSRLS)")
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
