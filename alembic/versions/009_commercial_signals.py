"""Tenant-scoped commercial evidence ledger. Additive migration; no customer data touched."""
from alembic import op
import sqlalchemy as sa
revision='009_commercial_signals'
down_revision='008_identity_security'
branch_labels=None
depends_on=None

def upgrade():
 op.create_table('commercial_signals',
  sa.Column('id',sa.String(36),primary_key=True),
  sa.Column('tenant_id',sa.String(36),sa.ForeignKey('tenants.id'),nullable=False),
  sa.Column('record_id',sa.String(36),nullable=False),
  sa.Column('actor_id',sa.String(36),nullable=False),
  sa.Column('kind',sa.String(40),nullable=False),
  sa.Column('source_type',sa.String(30),nullable=False),
  sa.Column('source_ref',sa.String(120),nullable=False),
  sa.Column('occurred_at',sa.DateTime(timezone=True),nullable=False),
  sa.Column('recorded_at',sa.DateTime(timezone=True),nullable=False,server_default=sa.text('CURRENT_TIMESTAMP')),
  sa.Column('verification',sa.String(30),nullable=False),
  sa.Column('status',sa.String(20),nullable=False,server_default='current'),
  sa.Column('supersedes_id',sa.String(36),nullable=True),
  sa.Column('detail',sa.String(300),nullable=False,server_default=''),
  sa.ForeignKeyConstraint(['tenant_id','record_id'],['crm_records.tenant_id','crm_records.id'],name='fk_signal_record_tenant'),
  sa.ForeignKeyConstraint(['tenant_id','actor_id'],['users.tenant_id','users.id'],name='fk_signal_actor_tenant'),
  sa.UniqueConstraint('tenant_id','source_type','source_ref',name='uq_signal_source'))
 op.create_index('ix_commercial_signals_tenant_id','commercial_signals',['tenant_id'])
 op.create_index('ix_signal_record_status','commercial_signals',['tenant_id','record_id','status','occurred_at'])
 if op.get_bind().dialect.name=='postgresql':
  op.execute('ALTER TABLE commercial_signals ENABLE ROW LEVEL SECURITY')
  op.execute("CREATE POLICY commercial_signals_tenant_isolation ON commercial_signals "
             "FOR ALL USING (tenant_id = NULLIF(current_setting('revenue_os.tenant_id', true), '')) "
             "WITH CHECK (tenant_id = NULLIF(current_setting('revenue_os.tenant_id', true), ''))")

def downgrade():
 if op.get_bind().dialect.name=='postgresql':
  op.execute('DROP POLICY IF EXISTS commercial_signals_tenant_isolation ON commercial_signals')
 op.drop_index('ix_signal_record_status',table_name='commercial_signals')
 op.drop_index('ix_commercial_signals_tenant_id',table_name='commercial_signals')
 op.drop_table('commercial_signals')
