"""OAuth2 PKCE storage, tenant RLS and encrypted credential persistence."""
from alembic import op
import sqlalchemy as sa
revision='011_crm_oauth'
down_revision='010_crm_sync_checkpoints'
branch_labels=None
depends_on=None

def upgrade():
 op.create_table('crm_oauth_pending',
  sa.Column('id',sa.String(36),primary_key=True),
  sa.Column('tenant_id',sa.String(36),sa.ForeignKey('tenants.id'),nullable=False),
  sa.Column('actor_id',sa.String(36),nullable=False),
  sa.Column('provider',sa.String(40),nullable=False),
  sa.Column('state_hash',sa.String(64),nullable=False,unique=True),
  sa.Column('verifier_ciphertext',sa.Text(),nullable=False),
  sa.Column('expires_at',sa.DateTime(timezone=True),nullable=False),
  sa.ForeignKeyConstraint(['tenant_id','actor_id'],['users.tenant_id','users.id'],name='fk_oauth_pending_actor_tenant'))
 op.create_index('ix_crm_oauth_pending_tenant_id','crm_oauth_pending',['tenant_id'])
 op.create_table('crm_oauth_connections',
  sa.Column('tenant_id',sa.String(36),sa.ForeignKey('tenants.id'),primary_key=True),
  sa.Column('provider',sa.String(40),primary_key=True),
  sa.Column('account_key',sa.String(180),nullable=False),
  sa.Column('token_ciphertext',sa.Text(),nullable=False),
  sa.Column('refresh_ciphertext',sa.Text(),nullable=True),
  sa.Column('expires_at',sa.DateTime(timezone=True),nullable=True),
  sa.Column('api_base',sa.String(250),nullable=False),
  sa.Column('scopes',sa.Text(),nullable=False,server_default=''),
  sa.Column('connected_at',sa.DateTime(timezone=True),nullable=False,server_default=sa.text('CURRENT_TIMESTAMP')))
 if op.get_bind().dialect.name=='postgresql':
  for table in ('crm_oauth_pending','crm_oauth_connections'):
   op.execute(f'ALTER TABLE {table} ENABLE ROW LEVEL SECURITY')
   op.execute(f"CREATE POLICY {table}_tenant_isolation ON {table} FOR ALL "
              "USING (tenant_id = NULLIF(current_setting('revenue_os.tenant_id', true), '')) "
              "WITH CHECK (tenant_id = NULLIF(current_setting('revenue_os.tenant_id', true), ''))")
def downgrade():
 op.drop_index('ix_crm_oauth_pending_tenant_id',table_name='crm_oauth_pending')
 for table in ('crm_oauth_connections','crm_oauth_pending'):
  if op.get_bind().dialect.name=='postgresql':
   op.execute(f'DROP POLICY IF EXISTS {table}_tenant_isolation ON {table}')
  op.drop_table(table)
