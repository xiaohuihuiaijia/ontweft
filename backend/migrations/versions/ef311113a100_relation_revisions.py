"""Retain confirmed relation attribute revisions when an edge is removed."""

import sqlalchemy as sa
from alembic import op

revision = "ef311113a100"
down_revision = "ef311111a100"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "relation_revisions",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("tenant_id", sa.String(36), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("project_id", sa.String(36), sa.ForeignKey("projects.id"), nullable=False),
        sa.Column("created_at", sa.String(40), nullable=False),
        sa.Column("relation_id", sa.String(36), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("type_key", sa.String(120), nullable=False),
        sa.Column("source_type_key", sa.String(120), nullable=False),
        sa.Column("source_id", sa.String(36), nullable=False),
        sa.Column("target_id", sa.String(36), nullable=False),
        sa.Column("properties", sa.JSON(), nullable=False),
        sa.Column("evidence", sa.JSON(), nullable=False),
        sa.Column("manual", sa.Boolean(), nullable=False),
        sa.Column("actor_id", sa.String(36), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("model_release_id", sa.String(36), sa.ForeignKey("releases.id"), nullable=True),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("operation", sa.String(20), nullable=False),
        sa.UniqueConstraint("relation_id", "revision", name="uq_relation_revision"),
    )
    for field in ("tenant_id", "project_id", "relation_id"):
        op.create_index(f"ix_relation_revisions_{field}", "relation_revisions", [field])
    if op.get_bind().dialect.name == "postgresql":
        op.execute("ALTER TABLE relation_revisions ENABLE ROW LEVEL SECURITY")
        op.execute("ALTER TABLE relation_revisions FORCE ROW LEVEL SECURITY")
        condition = "tenant_id = nullif(current_setting('app.tenant_id', true), '')"
        op.execute(
            f"CREATE POLICY tenant_isolation ON relation_revisions USING ({condition}) WITH CHECK ({condition})"
        )


def downgrade():
    raise RuntimeError("Preserve relation history before a downgrade")
