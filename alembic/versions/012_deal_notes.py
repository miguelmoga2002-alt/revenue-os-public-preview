"""Additive tenant-isolated deal notes with idempotency and author provenance."""
from alembic import op
import sqlalchemy as sa

revision='012_deal_notes'
down_revision='011_crm_oauth'
branch_labels=None
depends_on=None

def upgrade():
 op.create_table('deal_notes',
  sa.Column('id',sa.String(36),primary_key=True),
  sa.Column('tenant_id',sa.String(36),sa.ForeignKey('tenants.id'),nullable=False),
  sa.Column('deal_id',sa.String(36),nullable=False),
  sa.Column('author_id',sa.String(36),nullable=False),
  sa.Column('idem',sa.String(120),nullable=False),
  sa.Column('body',sa.Text(),nullable=False),
  sa.Column('created_at',sa.DateTime(timezone=True),nullable=False,server_default=sa.text('CURRENT_TIMESTAMP')),
  sa.ForeignKeyConstraint(['tenant_id','deal_id'],['crm_records.tenant_id','crm_records.id'],name='fk_deal_note_record_tenant'),
  sa.ForeignKeyConstraint(['tenant_id','author_id'],['users.tenant_id','users.id'],name='fk_deal_note_author_tenant'),
  sa.UniqueConstraint('tenant_id','author_id','idem',name='uq_deal_note_idem'))
 op.create_index('ix_deal_note_timeline','deal_notes',['tenant_id','deal_id','created_at'])
 op.create_index('ix_deal_notes_tenant_id','deal_notes',['tenant_id'])
 if op.get_bind().dialect.name=='postgresql':
  op.execute('ALTER TABLE deal_notes ENABLE ROW LEVEL SECURITY')
  op.execute("CREATE POLICY deal_notes_tenant_isolation ON deal_notes FOR ALL "
   "USING (tenant_id = NULLIF(current_setting('revenue_os.tenant_id', true), '')) "
   "WITH CHECK (tenant_id = NULLIF(current_setting('revenue_os.tenant_id', true), ''))")

def downgrade():
 if op.get_bind().dialect.name=='postgresql':
  op.execute('DROP POLICY IF EXISTS deal_notes_tenant_isolation ON deal_notes')
 op.drop_index('ix_deal_notes_tenant_id',table_name='deal_notes')
 op.drop_index('ix_deal_note_timeline',table_name='deal_notes')
 op.drop_table('deal_notes')
