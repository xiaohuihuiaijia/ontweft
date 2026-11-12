"""Bounded shared pre-authentication login counters; no user data is stored."""

import sqlalchemy as sa
from alembic import op

revision = "ef311029a500"
down_revision = "ef311029a400"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "login_throttles",
        sa.Column("key_hash", sa.String(64), primary_key=True),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("expires_at", sa.Float(), nullable=False),
    )
    op.create_index("ix_login_throttles_expires_at", "login_throttles", ["expires_at"])
    if op.get_bind().dialect.name == "postgresql":
        # Authentication precedes tenant resolution. Only opaque, short-lived
        # rate counters are global; tenant/user/session RLS policies stay intact.
        op.execute("ALTER TABLE login_throttles ENABLE ROW LEVEL SECURITY")
        op.execute("ALTER TABLE login_throttles FORCE ROW LEVEL SECURITY")
        op.execute("CREATE POLICY login_throttle_budget ON login_throttles USING (true) WITH CHECK (true)")


def downgrade():
    op.drop_table("login_throttles")
