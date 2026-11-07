"""Tenant-specific guided execution policies and durable tasks."""
from alembic import op
from app.models import Base
revision='002_guided'
down_revision='001_initial'
branch_labels=None
depends_on=None
TABLES=('work_policies','work_items','work_leases','work_results')
def upgrade():Base.metadata.create_all(bind=op.get_bind(),tables=[Base.metadata.tables[t] for t in TABLES])
def downgrade():Base.metadata.drop_all(bind=op.get_bind(),tables=[Base.metadata.tables[t] for t in reversed(TABLES)])
