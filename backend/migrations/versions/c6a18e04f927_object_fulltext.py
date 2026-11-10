"""Rebuildable per-field object full-text indexes and truthful checkpoints."""

import sqlalchemy as sa
from alembic import op

revision = "c6a18e04f927"
down_revision = "b9e5217ac340"
branch_labels = None
depends_on = None

TABLES = (
    "object_search_documents",
    "object_search_fields",
    "object_search_requests",
    "search_project_states",
    "search_cdc_checkpoints",
)


def common(prefix):
    return [
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("tenant_id", sa.String(36), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("project_id", sa.String(36), sa.ForeignKey("projects.id"), nullable=False),
        sa.Column("created_at", sa.String(40), nullable=False),
        sa.ForeignKeyConstraint(
            ["tenant_id", "project_id"],
            ["projects.tenant_id", "projects.id"],
            name=f"fk_search_{prefix}_project_scope",
        ),
    ]


def entity_scope(prefix):
    return sa.ForeignKeyConstraint(
        ["tenant_id", "project_id", "entity_id"],
        ["entities.tenant_id", "entities.project_id", "entities.id"],
        name=f"fk_search_{prefix}_entity_scope",
    )


def release_scope(prefix):
    return sa.ForeignKeyConstraint(
        ["tenant_id", "project_id", "release_id"],
        ["releases.tenant_id", "releases.project_id", "releases.id"],
        name=f"fk_search_{prefix}_release_scope",
    )


def upgrade():
    # Only add keys and empty derived tables. Existing revisions, native CDC
    # evidence, and all current watermarks remain unchanged; workers backfill.
    with op.batch_alter_table("projects") as batch:
        batch.create_unique_constraint("uq_project_scope", ["tenant_id", "id"])
    with op.batch_alter_table("entities") as batch:
        batch.create_unique_constraint("uq_entity_scope", ["tenant_id", "project_id", "id"])
    op.create_table(
        "object_search_documents",
        *common("document"),
        sa.Column("entity_id", sa.String(36), nullable=False),
        sa.Column("entity_revision", sa.Integer(), nullable=False),
        sa.Column("release_id", sa.String(36), nullable=False),
        sa.Column("generation", sa.String(36), nullable=False),
        sa.Column("tokenizer_version", sa.String(120), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("indexed_at", sa.String(40), nullable=False),
        sa.UniqueConstraint("entity_id", name="uq_search_document_entity"),
        entity_scope("document"),
        release_scope("document"),
        sa.CheckConstraint("entity_revision >= 1", name="ck_search_document_revision"),
        sa.CheckConstraint("status IN ('ready', 'removed', 'error')", name="ck_search_document_status"),
    )
    op.create_table(
        "object_search_fields",
        *common("field"),
        sa.Column("entity_id", sa.String(36), nullable=False),
        sa.Column("field_key", sa.String(120), nullable=False),
        sa.Column("chunk", sa.Integer(), nullable=False),
        sa.Column("token_text", sa.Text(), nullable=False),
        sa.Column("generation", sa.String(36), nullable=False),
        sa.UniqueConstraint("entity_id", "field_key", "chunk", name="uq_search_field_chunk"),
        entity_scope("field"),
        sa.CheckConstraint("chunk >= 0", name="ck_search_field_chunk"),
    )
    op.create_table(
        "object_search_requests",
        *common("request"),
        sa.Column("entity_id", sa.String(36), nullable=False),
        sa.Column("requested_revision", sa.Integer(), nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("updated_at", sa.String(40), nullable=False),
        sa.UniqueConstraint("entity_id", name="uq_search_request_entity"),
        entity_scope("request"),
        sa.CheckConstraint("requested_revision >= 1", name="ck_search_request_revision"),
    )
    op.create_table(
        "search_project_states",
        *common("state"),
        sa.Column("release_id", sa.String(36), nullable=True),
        sa.Column("generation", sa.String(36), nullable=False),
        sa.Column("tokenizer_version", sa.String(120), nullable=False),
        sa.Column("backfill_cursor", sa.String(36), nullable=False, server_default=""),
        sa.Column("backfill_complete", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("updated_at", sa.String(40), nullable=False),
        sa.UniqueConstraint("project_id", name="uq_search_state_project"),
        release_scope("state"),
    )
    op.create_table(
        "search_cdc_checkpoints",
        *common("checkpoint"),
        sa.Column("stream_id", sa.String(36), nullable=False),
        sa.Column("binding_id", sa.String(36), nullable=False),
        sa.Column("generation", sa.String(36), nullable=False),
        sa.Column("sequence", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("window_id", sa.String(36), nullable=True),
        sa.Column("updated_at", sa.String(40), nullable=False),
        sa.UniqueConstraint("stream_id", name="uq_search_checkpoint_stream"),
        sa.CheckConstraint("sequence >= 0", name="ck_search_checkpoint_sequence"),
        sa.ForeignKeyConstraint(
            ["tenant_id", "project_id", "stream_id"],
            ["cdc_streams.tenant_id", "cdc_streams.project_id", "cdc_streams.id"],
            name="fk_search_checkpoint_stream_scope",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "project_id", "stream_id", "binding_id"],
            [
                "cdc_projection_bindings.tenant_id",
                "cdc_projection_bindings.project_id",
                "cdc_projection_bindings.stream_id",
                "cdc_projection_bindings.id",
            ],
            name="fk_search_checkpoint_binding_scope",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "project_id", "stream_id", "binding_id", "window_id"],
            [
                "cdc_projection_windows.tenant_id",
                "cdc_projection_windows.project_id",
                "cdc_projection_windows.stream_id",
                "cdc_projection_windows.binding_id",
                "cdc_projection_windows.id",
            ],
            name="fk_search_checkpoint_window_scope",
        ),
    )
    postgres = op.get_bind().dialect.name == "postgresql"
    if postgres:
        op.create_index(
            "ix_object_search_fields_fts",
            "object_search_fields",
            [sa.text("to_tsvector('simple'::regconfig, token_text)")],
            postgresql_using="gin",
        )
    for table in TABLES:
        for column in ("tenant_id", "project_id"):
            op.create_index(f"ix_{table}_{column}", table, [column])
        if postgres:
            op.execute(f'ALTER TABLE "{table}" ENABLE ROW LEVEL SECURITY')
            op.execute(f'ALTER TABLE "{table}" FORCE ROW LEVEL SECURITY')
            condition = "tenant_id = nullif(current_setting('app.tenant_id', true), '')"
            op.execute(
                f'CREATE POLICY tenant_isolation ON "{table}" USING ({condition}) WITH CHECK ({condition})'
            )
    # Deliberately no immutable-history trigger: these are replaceable derived
    # rows. Authorization, evidence and source history remain in their owners.


def downgrade():
    raise RuntimeError("Search rollback requires reviewed removal of the derived tables and scope keys")
