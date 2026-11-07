"""Initial isolated v1.0 schema."""
from alembic import op
from app.models import Base
from sqlalchemy import MetaData
revision='001_initial'
down_revision=None
branch_labels=None
depends_on=None
def upgrade():
 names=[t for t in Base.metadata.tables.values() if t.name not in ("work_policies","work_items","work_leases","work_results","crm_links","record_grants","pipeline_stages","task_create_receipts","admin_totp","auth_login_attempts","session_proofs","commercial_signals","crm_sync_checkpoints","crm_oauth_pending","crm_oauth_connections","deal_notes")]
 Base.metadata.create_all(bind=op.get_bind(),tables=names)
def downgrade():
 names=[t for t in Base.metadata.tables.values() if t.name not in ("work_policies","work_items","work_leases","work_results","admin_totp","auth_login_attempts","session_proofs","commercial_signals","crm_sync_checkpoints","deal_notes")]
 Base.metadata.drop_all(bind=op.get_bind(),tables=names)
