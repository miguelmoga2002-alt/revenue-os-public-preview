"""Add record-level ACL grants with DB-enforced tenant-bound foreign keys."""
from alembic import op
import sqlalchemy as sa

revision = "005_record_grants"
down_revision = "004_crm_links"
branch_labels = None
depends_on = None


def upgrade():
    db = op.get_bind()
    existing = {c["name"] for c in sa.inspect(db).get_unique_constraints("users")}
    if "uq_user_tenant_id" not in existing:
        if db.dialect.name == "sqlite":
            with op.batch_alter_table("users") as batch:
                batch.create_unique_constraint("uq_user_tenant_id", ["tenant_id", "id"])
        else:
            op.create_unique_constraint("uq_user_tenant_id", "users", ["tenant_id", "id"])

    op.create_table(
        "record_grants",
        sa.Column("tenant_id", sa.String(36), sa.ForeignKey("tenants.id"), primary_key=True),
        sa.Column("user_id", sa.String(36), primary_key=True),
        sa.Column("record_id", sa.String(36), primary_key=True),
        sa.ForeignKeyConstraint(
            ["tenant_id", "user_id"], ["users.tenant_id", "users.id"],
            name="fk_grant_user_tenant", ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "record_id"], ["crm_records.tenant_id", "crm_records.id"],
            name="fk_grant_record_tenant", ondelete="CASCADE"
        ),
    )
    op.create_index("ix_record_grants_record", "record_grants", ["tenant_id", "record_id"])
    if db.dialect.name == "postgresql":
        op.execute("ALTER TABLE record_grants ENABLE ROW LEVEL SECURITY")
        op.execute(
            "CREATE POLICY record_grants_tenant_isolation ON record_grants FOR ALL "
            "USING (tenant_id = NULLIF(current_setting('revenue_os.tenant_id', true), '')) "
            "WITH CHECK (tenant_id = NULLIF(current_setting('revenue_os.tenant_id', true), ''))"
        )


def downgrade():
    db = op.get_bind()
    if db.dialect.name == "postgresql":
        op.execute("DROP POLICY IF EXISTS record_grants_tenant_isolation ON record_grants")
    op.drop_index("ix_record_grants_record", table_name="record_grants")
    op.drop_table("record_grants")
    if db.dialect.name == "sqlite":
        with op.batch_alter_table("users") as batch:
            batch.drop_constraint("uq_user_tenant_id", type_="unique")
    else:
        op.drop_constraint("uq_user_tenant_id", "users", type_="unique")
