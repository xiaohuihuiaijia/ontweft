"""Let bounded authorized search return the next page without sorting all objects."""

import sqlalchemy as sa
from alembic import op

revision = "ef311029a700"
down_revision = "ef311029a600"
branch_labels = None
depends_on = None


def upgrade():
    op.create_index(
        "ix_entity_browse",
        "entities",
        ["tenant_id", "project_id", sa.text("updated_at DESC"), "id"],
        postgresql_where=sa.text("deleted IS FALSE AND data_status = 'active'"),
        sqlite_where=sa.text("deleted IS FALSE AND data_status = 'active'"),
    )


def downgrade():
    op.drop_index("ix_entity_browse", table_name="entities")
