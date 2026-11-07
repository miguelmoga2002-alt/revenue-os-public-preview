"""Idempotent manager-created Focus tasks with tenant scoped durable receipts.

A unique tenant+key receipt prevents duplicate task/audit/grant writes even
when two managers or app workers submit the same request concurrently.
"""
from alembic import op
import sqlalchemy as sa

revision = "007_task_create_receipts"
down_revision = "006_pipeline_stages"
branch_labels = None
depends_on = None

def upgrade():
    op.create_table(
        "task_create_receipts",
        sa.Column("tenant_id", sa.String(36), sa.ForeignKey("tenants.id"), primary_key=True),
        sa.Column("idem", sa.String(120), primary_key=True),
        sa.Column("actor_id", sa.String(36), nullable=False),
        sa.Column("task_id", sa.String(36), sa.ForeignKey("work_items.id"), nullable=False, unique=True),
        sa.Column("payload_sha", sa.String(64), nullable=False),
        sa.ForeignKeyConstraint(
            ["tenant_id", "actor_id"], ["users.tenant_id", "users.id"],
            name="fk_task_receipt_actor_tenant",
        ),
    )
    if op.get_bind().dialect.name == "postgresql":
        op.execute("ALTER TABLE task_create_receipts ENABLE ROW LEVEL SECURITY")
        op.execute(
            "CREATE POLICY task_create_receipts_tenant_isolation ON task_create_receipts "
            "FOR ALL USING (tenant_id = NULLIF(current_setting('revenue_os.tenant_id', true), '')) "
            "WITH CHECK (tenant_id = NULLIF(current_setting('revenue_os.tenant_id', true), ''))"
        )

def downgrade():
    if op.get_bind().dialect.name == "postgresql":
        op.execute("DROP POLICY IF EXISTS task_create_receipts_tenant_isolation ON task_create_receipts")
    op.drop_table("task_create_receipts")
