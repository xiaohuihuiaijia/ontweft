"""Keep model explanations with their proposal, separate from published definitions."""

import sqlalchemy as sa
from alembic import op

revision = "ef311113a200"
down_revision = "ef311113a100"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("ai_proposals", sa.Column("model_notes", sa.JSON(), nullable=False, server_default="[]"))


def downgrade():
    op.drop_column("ai_proposals", "model_notes")
