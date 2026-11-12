"""Business-key reservations and a monotonic model-validation watermark."""

import sqlalchemy as sa
from alembic import op

revision = "ef311029a100"
down_revision = "b4a2d7e9c601"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("projects", sa.Column("data_revision", sa.BigInteger(), nullable=False, server_default="0"))
    op.add_column("entities", sa.Column("changed_seq", sa.BigInteger(), nullable=False, server_default="0"))
    op.create_index("ix_entity_changes", "entities", ["tenant_id", "project_id", "changed_seq", "id"])
    op.create_table(
        "entity_keys",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("tenant_id", sa.String(36), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("project_id", sa.String(36), sa.ForeignKey("projects.id"), nullable=False),
        sa.Column("created_at", sa.String(40), nullable=False),
        sa.Column("definition_id", sa.String(36), nullable=False),
        sa.Column("entity_id", sa.String(36), nullable=False),
        sa.Column("type_key", sa.String(120), nullable=False),
        sa.Column("fields_hash", sa.String(64), nullable=False),
        sa.Column("value_hash", sa.String(64), nullable=False),
        sa.ForeignKeyConstraint(
            ["tenant_id", "project_id", "entity_id"],
            ["entities.tenant_id", "entities.project_id", "entities.id"],
            name="fk_entity_key_scope",
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "project_id",
            "definition_id",
            "type_key",
            "fields_hash",
            "value_hash",
            name="uq_entity_business_key",
        ),
    )
    for field in ("tenant_id", "project_id"):
        op.create_index("ix_entity_keys_" + field, "entity_keys", [field])
    op.create_index(
        "ix_entity_key_owner", "entity_keys", ["tenant_id", "project_id", "definition_id", "entity_id"]
    )
    if op.get_bind().dialect.name == "postgresql":
        op.execute("ALTER TABLE entity_keys ENABLE ROW LEVEL SECURITY")
        op.execute("ALTER TABLE entity_keys FORCE ROW LEVEL SECURITY")
        condition = "tenant_id = nullif(current_setting('app.tenant_id', true), '')"
        op.execute(
            f"CREATE POLICY tenant_isolation ON entity_keys USING ({condition}) WITH CHECK ({condition})"
        )


def downgrade():
    op.drop_table("entity_keys")
    op.drop_index("ix_entity_changes", table_name="entities")
    op.drop_column("entities", "changed_seq")
    op.drop_column("projects", "data_revision")
