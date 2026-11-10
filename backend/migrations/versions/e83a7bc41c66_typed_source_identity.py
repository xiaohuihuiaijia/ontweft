"""Preserve raw keys while versioning canonical source identities."""
from alembic import op
import sqlalchemy as sa

revision = "e83a7bc41c66"
down_revision = "6b79ed866ac4"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("source_record_revisions", sa.Column("identity_key", sa.JSON(), nullable=True))
    op.add_column("source_identities", sa.Column("key_codec", sa.String(30), nullable=False, server_default="legacy-json-v1"))


def downgrade():
    # Rewriting canonical hashes to legacy hashes cannot safely be inferred here.
    raise RuntimeError("Identity codec downgrade requires a reviewed identity migration")
