"""Internal CRM links with composite tenant foreign keys.

Separate table avoids rewriting existing CRM rows; records keep external IDs.
"""
from alembic import op
import sqlalchemy as sa

revision = "004_crm_links"
down_revision = "003_postgres_rls"
branch_labels = None
depends_on = None


def upgrade():
    columns = {c["name"] for c in sa.inspect(op.get_bind()).get_columns("crm_records")}
    if "deal_external_id" not in columns:
        op.add_column("crm_records", sa.Column("deal_external_id", sa.String(180), nullable=True))
    # Historical 001 uses current ORM metadata: a fresh DB may already
    # contain this constraint. Existing v1.0 databases do not.
    existing = {u.get("name") for u in sa.inspect(op.get_bind()).get_unique_constraints("crm_records")}
    if "uq_crm_tenant_id" not in existing:
        if op.get_bind().dialect.name == "sqlite":
            with op.batch_alter_table("crm_records") as batch:
                batch.create_unique_constraint("uq_crm_tenant_id", ["tenant_id", "id"])
        else:
            op.create_unique_constraint("uq_crm_tenant_id", "crm_records", ["tenant_id", "id"])
    op.create_table(
        "crm_links",
        sa.Column("tenant_id", sa.String(36), sa.ForeignKey("tenants.id"), primary_key=True),
        sa.Column("record_id", sa.String(36), primary_key=True),
        sa.Column("relation", sa.String(20), primary_key=True),
        sa.Column("related_id", sa.String(36), nullable=False),
        sa.ForeignKeyConstraint(["tenant_id", "record_id"], ["crm_records.tenant_id", "crm_records.id"], name="fk_crm_link_source_tenant", ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["tenant_id", "related_id"], ["crm_records.tenant_id", "crm_records.id"], name="fk_crm_link_related_tenant", ondelete="CASCADE"),
    )
    op.create_index("ix_crm_link_related", "crm_links", ["tenant_id", "related_id"])
    if op.get_bind().dialect.name == "postgresql":
        op.execute("ALTER TABLE crm_links ENABLE ROW LEVEL SECURITY")
        op.execute(
            "CREATE POLICY crm_links_tenant_isolation ON crm_links FOR ALL "
            "USING (tenant_id = NULLIF(current_setting('revenue_os.tenant_id', true), '')) "
            "WITH CHECK (tenant_id = NULLIF(current_setting('revenue_os.tenant_id', true), ''))"
        )


def downgrade():
    if op.get_bind().dialect.name == "postgresql":
        op.execute("DROP POLICY IF EXISTS crm_links_tenant_isolation ON crm_links")
    op.drop_index("ix_crm_link_related", table_name="crm_links")
    op.drop_table("crm_links")
    if op.get_bind().dialect.name == "sqlite":
        with op.batch_alter_table("crm_records") as batch:
            batch.drop_constraint("uq_crm_tenant_id", type_="unique")
            batch.drop_column("deal_external_id")
    else:
        op.drop_constraint("uq_crm_tenant_id", "crm_records", type_="unique")
        op.drop_column("crm_records", "deal_external_id")
