"""Transaction-local PostgreSQL RLS context set only from verified user identity.

The setting must never originate from user-supplied tenant headers or request JSON.
With SQLite there is no RLS; application query filters remain mandatory.
"""
from sqlalchemy import text
from sqlalchemy.orm import Session


def set_tenant_context(db: Session, tenant_id: str) -> None:
    if not tenant_id:
        raise ValueError("Tenant identity required")
    if db.get_bind().dialect.name == "postgresql":
        db.execute(
            text("SELECT set_config('revenue_os.tenant_id', :tenant_id, true)"),
            {"tenant_id": tenant_id},
        )
