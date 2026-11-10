"""Version-bound document and published knowledge full-text projections."""

import sqlalchemy as sa
from alembic import op

revision = "e8a42b6d901c"
down_revision = "c6a18e04f927"
branch_labels = None
depends_on = None

TABLES = (
    "content_search_states",
    "content_search_documents",
    "content_search_chunks",
    "content_search_requests",
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
            name=f"fk_content_search_{prefix}_project_scope",
        ),
    ]


def asset_columns():
    return [
        sa.Column("asset_kind", sa.String(20), nullable=False),
        sa.Column("asset_id", sa.String(36), nullable=False),
        sa.Column("document_id", sa.String(36), nullable=True),
        sa.Column("page_id", sa.String(36), nullable=True),
    ]


def asset_constraints(prefix, *, revision=False):
    document_branch = (
        "asset_kind = 'document' AND document_id IS NOT NULL AND asset_id = document_id AND page_id IS NULL"
    )
    knowledge_branch = (
        "asset_kind = 'knowledge' AND page_id IS NOT NULL AND asset_id = page_id AND document_id IS NULL"
    )
    if revision:
        document_branch += " AND knowledge_revision_id IS NULL"
        knowledge_branch += " AND knowledge_revision_id IS NOT NULL"
    return [
        sa.UniqueConstraint("asset_kind", "asset_id", name=f"uq_content_search_{prefix}_asset"),
        sa.CheckConstraint(
            f"({document_branch}) OR ({knowledge_branch})", name=f"ck_content_search_{prefix}_asset"
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "project_id", "document_id"],
            ["documents.tenant_id", "documents.project_id", "documents.id"],
            name=f"fk_content_search_{prefix}_document_scope",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "project_id", "page_id"],
            ["knowledge_pages.tenant_id", "knowledge_pages.project_id", "knowledge_pages.id"],
            name=f"fk_content_search_{prefix}_page_scope",
        ),
    ]


def upgrade():
    # Existing IRs and published history remain byte-for-byte unchanged. Zero
    # means no versioned parse result has been certified by the new worker yet.
    with op.batch_alter_table("documents") as batch:
        batch.add_column(sa.Column("parse_revision", sa.Integer(), nullable=False, server_default="0"))
        batch.add_column(sa.Column("ir_fingerprint", sa.String(64), nullable=True))
        batch.create_unique_constraint("uq_document_scope", ["tenant_id", "project_id", "id"])
        batch.create_check_constraint("ck_document_parse_revision", "parse_revision >= 0")
    with op.batch_alter_table("knowledge_pages") as batch:
        batch.create_unique_constraint("uq_knowledge_page_scope", ["tenant_id", "project_id", "id"])
    with op.batch_alter_table("knowledge_revisions") as batch:
        batch.create_unique_constraint(
            "uq_knowledge_revision_page_scope", ["tenant_id", "project_id", "page_id", "id"]
        )

    op.create_table(
        "content_search_states",
        *common("state"),
        sa.Column("kind", sa.String(20), nullable=False),
        sa.Column("generation", sa.String(36), nullable=False),
        sa.Column("tokenizer_version", sa.String(120), nullable=False),
        sa.Column("chunker_version", sa.String(120), nullable=False),
        sa.Column("backfill_cursor", sa.String(36), nullable=False, server_default=""),
        sa.Column("backfill_complete", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("updated_at", sa.String(40), nullable=False),
        sa.UniqueConstraint("project_id", "kind", name="uq_content_search_state_project_kind"),
        sa.CheckConstraint("kind IN ('document', 'knowledge')", name="ck_content_search_state_kind"),
    )
    op.create_table(
        "content_search_documents",
        *common("document"),
        *asset_columns(),
        sa.Column("knowledge_revision_id", sa.String(36), nullable=True),
        sa.Column("source_revision", sa.Integer(), nullable=False),
        sa.Column("parse_revision", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("source_fingerprint", sa.String(64), nullable=False),
        sa.Column("title", sa.String(250), nullable=False),
        sa.Column("coverage", sa.JSON(), nullable=False),
        sa.Column("generation", sa.String(36), nullable=False),
        sa.Column("tokenizer_version", sa.String(120), nullable=False),
        sa.Column("chunker_version", sa.String(120), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("indexed_at", sa.String(40), nullable=False),
        *asset_constraints("document", revision=True),
        sa.UniqueConstraint("tenant_id", "project_id", "id", name="uq_content_search_document_scope"),
        sa.ForeignKeyConstraint(
            ["tenant_id", "project_id", "page_id", "knowledge_revision_id"],
            [
                "knowledge_revisions.tenant_id",
                "knowledge_revisions.project_id",
                "knowledge_revisions.page_id",
                "knowledge_revisions.id",
            ],
            name="fk_content_search_document_revision_scope",
        ),
        sa.CheckConstraint("source_revision >= 1", name="ck_content_search_document_revision"),
        sa.CheckConstraint("parse_revision >= 0", name="ck_content_search_document_parse_revision"),
        sa.CheckConstraint(
            "status IN ('ready', 'partial', 'removed', 'error')", name="ck_content_search_document_status"
        ),
    )
    op.create_table(
        "content_search_chunks",
        *common("chunk"),
        sa.Column("receipt_id", sa.String(36), nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("token_text", sa.Text(), nullable=False),
        sa.Column("anchor", sa.JSON(), nullable=False),
        sa.UniqueConstraint("receipt_id", "ordinal", name="uq_content_search_chunk_ordinal"),
        sa.CheckConstraint("ordinal >= 0", name="ck_content_search_chunk_ordinal"),
        sa.ForeignKeyConstraint(
            ["tenant_id", "project_id", "receipt_id"],
            [
                "content_search_documents.tenant_id",
                "content_search_documents.project_id",
                "content_search_documents.id",
            ],
            name="fk_content_search_chunk_receipt_scope",
        ),
    )
    op.create_table(
        "content_search_requests",
        *common("request"),
        *asset_columns(),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("updated_at", sa.String(40), nullable=False),
        *asset_constraints("request"),
    )
    postgres = op.get_bind().dialect.name == "postgresql"
    if postgres:
        op.create_index(
            "ix_content_search_chunks_fts",
            "content_search_chunks",
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
    # Derived tables deliberately remain mutable. Runtime grants are assigned
    # from registered metadata by the existing provisioning procedure.


def downgrade():
    raise RuntimeError(
        "Content search rollback requires reviewed removal of derived tables and parse metadata"
    )
