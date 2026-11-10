"""Optional enterprise-scoped usernames; retain existing email credentials."""

import sqlalchemy as sa
from alembic import op

revision = "b4a2d7e9c601"
down_revision = "d52e0c98a46b"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("users", sa.Column("username", sa.String(64), nullable=True))
    op.create_index("uq_user_username", "users", ["tenant_id", "username"], unique=True)


def downgrade():
    op.drop_index("uq_user_username", table_name="users")
    op.drop_column("users", "username")
