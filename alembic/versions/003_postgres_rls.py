"""Enable tenant RLS on PostgreSQL business tables (no-op on SQLite).

Important: the production app must use a non-owner, non-BYPASSRLS role.
No database role is created by the migration: provision separately.
"""
from alembic import op

revision = "003_postgres_rls"
down_revision = "002_guided"
branch_labels = None
depends_on = None

TENANT_TABLES = (
    "crm_records",
    "import_batches",
    "batch_mutations",
    "audit_events",
    "work_policies",
    "work_items",
    "work_leases",
    "work_results",
)
PREDICATE = (
    "tenant_id = NULLIF(current_setting('revenue_os.tenant_id', true), '')"
)


def upgrade():
    if op.get_bind().dialect.name != "postgresql":
        return
    for table in TENANT_TABLES:
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(
            f"CREATE POLICY {table}_tenant_isolation ON {table} "
            f"FOR ALL USING ({PREDICATE}) WITH CHECK ({PREDICATE})"
        )


def downgrade():
    if op.get_bind().dialect.name != "postgresql":
        return
    for table in reversed(TENANT_TABLES):
        op.execute(f"DROP POLICY IF EXISTS {table}_tenant_isolation ON {table}")
        op.execute(f"ALTER TABLE {table} DISABLE ROW LEVEL SECURITY")
