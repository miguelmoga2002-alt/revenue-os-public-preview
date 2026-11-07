"""Tenant-configured stages in the single-pipeline CRM workspace."""
from alembic import op
import sqlalchemy as sa
revision = "006_pipeline_stages"
down_revision = "005_record_grants"
branch_labels = None
depends_on = None

def upgrade():
    op.create_table(
        "pipeline_stages",
        sa.Column("tenant_id", sa.String(36), sa.ForeignKey("tenants.id"), primary_key=True),
        sa.Column("name", sa.String(80), primary_key=True),
        sa.Column("position", sa.Integer(), nullable=False))
    op.create_index("ix_pipeline_stages_tenant_position", "pipeline_stages", ["tenant_id", "position"])
    if op.get_bind().dialect.name == "postgresql":
        op.execute("ALTER TABLE pipeline_stages ENABLE ROW LEVEL SECURITY")
        op.execute(
            "CREATE POLICY pipeline_stages_tenant_isolation ON pipeline_stages FOR ALL "
            "USING (tenant_id = NULLIF(current_setting('revenue_os.tenant_id', true), '')) "
            "WITH CHECK (tenant_id = NULLIF(current_setting('revenue_os.tenant_id', true), ''))")

def downgrade():
    if op.get_bind().dialect.name == "postgresql":
        op.execute("DROP POLICY IF EXISTS pipeline_stages_tenant_isolation ON pipeline_stages")
    op.drop_index("ix_pipeline_stages_tenant_position", table_name="pipeline_stages")
    op.drop_table("pipeline_stages")
