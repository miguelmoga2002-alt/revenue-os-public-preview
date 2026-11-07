"""Additive checkpoint metadata for manual Pipedrive v2 read-only sync."""
from alembic import op
import sqlalchemy as sa
revision="010_crm_sync_checkpoints"
down_revision="009_commercial_signals"
branch_labels=None
depends_on=None

def upgrade():
 op.create_table("crm_sync_checkpoints",
  sa.Column("tenant_id",sa.String(36),sa.ForeignKey("tenants.id"),primary_key=True),
  sa.Column("provider",sa.String(40),primary_key=True),
  sa.Column("kind",sa.String(20),primary_key=True),
  sa.Column("last_completed_until",sa.DateTime(timezone=True),nullable=False),
  sa.Column("last_completed_at",sa.DateTime(timezone=True),nullable=False,server_default=sa.text("CURRENT_TIMESTAMP")),
  sa.Column("imported_count",sa.Integer(),nullable=False,server_default="0"))
 if op.get_bind().dialect.name=="postgresql":
  op.execute("ALTER TABLE crm_sync_checkpoints ENABLE ROW LEVEL SECURITY")
  op.execute("CREATE POLICY crm_sync_checkpoints_tenant_isolation ON crm_sync_checkpoints "
             "FOR ALL USING (tenant_id = NULLIF(current_setting('revenue_os.tenant_id', true), '')) "
             "WITH CHECK (tenant_id = NULLIF(current_setting('revenue_os.tenant_id', true), ''))")

def downgrade():
 if op.get_bind().dialect.name=="postgresql":
  op.execute("DROP POLICY IF EXISTS crm_sync_checkpoints_tenant_isolation ON crm_sync_checkpoints")
 op.drop_table("crm_sync_checkpoints")
