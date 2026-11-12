"""Model connections and reviewable AI proposals, preserving existing releases."""

import sqlalchemy as sa
from alembic import op

revision = "ef311111a100"
down_revision = "ef311029a700"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "model_connections",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("tenant_id", sa.String(length=36), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("base_url", sa.String(length=2048), nullable=False),
        sa.Column("secret", sa.Text(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("allow_private_network", sa.Boolean(), nullable=False),
        sa.Column("models", sa.JSON(), nullable=False),
        sa.Column("roles", sa.JSON(), nullable=False),
        sa.Column("last_test", sa.JSON(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.String(length=40), nullable=False),
        sa.Column("updated_at", sa.String(length=40), nullable=False),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_model_connections_tenant_id"), "model_connections", ["tenant_id"], unique=False)
    op.create_table(
        "ai_proposals",
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=24), nullable=False),
        sa.Column("owner_id", sa.String(length=36), nullable=False),
        sa.Column("connection_id", sa.String(length=36), nullable=False),
        sa.Column("connection_revision", sa.Integer(), nullable=False),
        sa.Column("baseline_release_id", sa.String(length=36), nullable=True),
        sa.Column("inputs", sa.JSON(), nullable=False),
        sa.Column("definition", sa.JSON(), nullable=False),
        sa.Column("candidates", sa.JSON(), nullable=False),
        sa.Column("relations", sa.JSON(), nullable=False),
        sa.Column("gaps", sa.JSON(), nullable=False),
        sa.Column("job_id", sa.String(length=36), nullable=True),
        sa.Column("changeset_id", sa.String(length=36), nullable=True),
        sa.Column("source_id", sa.String(length=36), nullable=True),
        sa.Column("imported", sa.JSON(), nullable=False),
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("tenant_id", sa.String(length=36), nullable=False),
        sa.Column("project_id", sa.String(length=36), nullable=False),
        sa.Column("created_at", sa.String(length=40), nullable=False),
        sa.ForeignKeyConstraint(
            ["baseline_release_id"],
            ["releases.id"],
        ),
        sa.ForeignKeyConstraint(
            ["changeset_id"],
            ["changesets.id"],
        ),
        sa.ForeignKeyConstraint(
            ["connection_id"],
            ["model_connections.id"],
        ),
        sa.ForeignKeyConstraint(
            ["job_id"],
            ["jobs.id"],
        ),
        sa.ForeignKeyConstraint(
            ["owner_id"],
            ["users.id"],
        ),
        sa.ForeignKeyConstraint(
            ["project_id"],
            ["projects.id"],
        ),
        sa.ForeignKeyConstraint(
            ["source_id"],
            ["sources.id"],
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_ai_proposals_project_id"), "ai_proposals", ["project_id"], unique=False)
    op.create_index(op.f("ix_ai_proposals_tenant_id"), "ai_proposals", ["tenant_id"], unique=False)
    op.create_table(
        "ai_document_pages",
        sa.Column("document_id", sa.String(length=36), nullable=False),
        sa.Column("source_revision", sa.Integer(), nullable=False),
        sa.Column("page_number", sa.Integer(), nullable=False),
        sa.Column("cache_key", sa.String(length=64), nullable=False),
        sa.Column("model", sa.String(length=200), nullable=False),
        sa.Column("result", sa.JSON(), nullable=False),
        sa.Column("usage", sa.JSON(), nullable=False),
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("tenant_id", sa.String(length=36), nullable=False),
        sa.Column("project_id", sa.String(length=36), nullable=False),
        sa.Column("created_at", sa.String(length=40), nullable=False),
        sa.ForeignKeyConstraint(
            ["document_id"],
            ["documents.id"],
        ),
        sa.ForeignKeyConstraint(
            ["project_id"],
            ["projects.id"],
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("tenant_id", "project_id", "cache_key", name="uq_ai_page_cache"),
    )
    op.create_index(
        op.f("ix_ai_document_pages_document_id"), "ai_document_pages", ["document_id"], unique=False
    )
    op.create_index(
        op.f("ix_ai_document_pages_project_id"), "ai_document_pages", ["project_id"], unique=False
    )
    op.create_index(op.f("ix_ai_document_pages_tenant_id"), "ai_document_pages", ["tenant_id"], unique=False)
    op.create_table(
        "document_relation_contributions",
        sa.Column("source_id", sa.String(length=36), nullable=False),
        sa.Column("fact_key", sa.String(length=64), nullable=False),
        sa.Column("proposal_id", sa.String(length=36), nullable=False),
        sa.Column("relation_id", sa.String(length=36), nullable=False),
        sa.Column("evidence", sa.JSON(), nullable=False),
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("tenant_id", sa.String(length=36), nullable=False),
        sa.Column("project_id", sa.String(length=36), nullable=False),
        sa.Column("created_at", sa.String(length=40), nullable=False),
        sa.ForeignKeyConstraint(
            ["project_id"],
            ["projects.id"],
        ),
        sa.ForeignKeyConstraint(
            ["proposal_id"],
            ["ai_proposals.id"],
        ),
        sa.ForeignKeyConstraint(
            ["relation_id"],
            ["relations.id"],
        ),
        sa.ForeignKeyConstraint(
            ["source_id"],
            ["sources.id"],
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("source_id", "fact_key", name="uq_document_relation_fact"),
    )
    op.create_index(
        op.f("ix_document_relation_contributions_project_id"),
        "document_relation_contributions",
        ["project_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_document_relation_contributions_relation_id"),
        "document_relation_contributions",
        ["relation_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_document_relation_contributions_source_id"),
        "document_relation_contributions",
        ["source_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_document_relation_contributions_tenant_id"),
        "document_relation_contributions",
        ["tenant_id"],
        unique=False,
    )

    if op.get_bind().dialect.name == "postgresql":
        for table in [
            "model_connections",
            "ai_proposals",
            "ai_document_pages",
            "document_relation_contributions",
        ]:
            op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
            op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
            condition = "tenant_id = nullif(current_setting('app.tenant_id', true), '')"
            op.execute(
                f"CREATE POLICY tenant_isolation ON {table} USING ({condition}) WITH CHECK ({condition})"
            )


def downgrade():
    raise RuntimeError("Preserve model credentials and proposal evidence before a downgrade")
