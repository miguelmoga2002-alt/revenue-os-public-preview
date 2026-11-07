"""Administrative MFA, distributed per-account login throttling and session proof.

Additive migration: preserves all legacy v1.0 sessions, but these sessions
cannot pass MFA-gated admin authorization without a new MFA login.
"""
from alembic import op
import sqlalchemy as sa

revision = "008_identity_security"
down_revision = "007_task_create_receipts"
branch_labels = None
depends_on = None

TABLES = ("admin_totp", "auth_login_attempts", "session_proofs")


def upgrade():
    op.create_table(
        "admin_totp",
        sa.Column("tenant_id", sa.String(36), sa.ForeignKey("tenants.id"), primary_key=True),
        sa.Column("user_id", sa.String(36), primary_key=True),
        sa.Column("secret_ciphertext", sa.Text(), nullable=False),
        sa.Column("last_step", sa.Integer(), nullable=False, server_default="-1"),
        sa.ForeignKeyConstraint(["tenant_id", "user_id"], ["users.tenant_id", "users.id"],
                                name="fk_totp_user_tenant"),
    )
    op.create_table(
        "auth_login_attempts",
        sa.Column("tenant_id", sa.String(36), sa.ForeignKey("tenants.id"), primary_key=True),
        sa.Column("user_id", sa.String(36), primary_key=True),
        sa.Column("failures", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("window_started_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.Column("locked_until", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["tenant_id", "user_id"], ["users.tenant_id", "users.id"],
                                name="fk_auth_attempt_user_tenant"),
    )
    op.create_table(
        "session_proofs",
        sa.Column("token_hash", sa.String(64), sa.ForeignKey("sessions.token_hash", ondelete="CASCADE"), primary_key=True),
        sa.Column("tenant_id", sa.String(36), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("mfa_verified", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("CURRENT_TIMESTAMP")),
    )
    op.create_index("ix_session_proofs_tenant_id", "session_proofs", ["tenant_id"])
    if op.get_bind().dialect.name == "postgresql":
        pred = "tenant_id = NULLIF(current_setting('revenue_os.tenant_id', true), '')"
        for table in TABLES:
            op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
            op.execute(f"CREATE POLICY {table}_tenant_isolation ON {table} "
                       f"FOR ALL USING ({pred}) WITH CHECK ({pred})")


def downgrade():
    if op.get_bind().dialect.name == "postgresql":
        for table in reversed(TABLES):
            op.execute(f"DROP POLICY IF EXISTS {table}_tenant_isolation ON {table}")
    op.drop_index("ix_session_proofs_tenant_id", table_name="session_proofs")
    for table in reversed(TABLES):
        op.drop_table(table)
