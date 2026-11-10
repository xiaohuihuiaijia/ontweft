"""Immutable document parse versions without manufacturing legacy history."""

import sqlalchemy as sa
from alembic import op

revision = "f2c7d9a41b60"
down_revision = "e8a42b6d901c"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("jobs") as batch:
        batch.create_unique_constraint("uq_job_scope", ["tenant_id", "project_id", "id"])
    op.create_table(
        "document_parse_versions",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("tenant_id", sa.String(36), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("project_id", sa.String(36), sa.ForeignKey("projects.id"), nullable=False),
        sa.Column("created_at", sa.String(40), nullable=False),
        sa.Column("document_id", sa.String(36), nullable=False),
        sa.Column("source_revision", sa.Integer(), nullable=False),
        sa.Column("parse_revision", sa.Integer(), nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("ir_fingerprint", sa.String(64), nullable=False),
        sa.Column("ir", sa.JSON(none_as_null=True), nullable=False),
        sa.Column("parser", sa.String(120), nullable=False),
        sa.Column("parser_version", sa.String(120), nullable=False),
        sa.Column("configuration", sa.JSON(), nullable=False),
        sa.Column("configuration_fingerprint", sa.String(64), nullable=False),
        sa.Column("engine", sa.String(20), nullable=False),
        sa.Column("origin", sa.String(20), nullable=False),
        sa.Column("job_id", sa.String(36), nullable=True),
        sa.UniqueConstraint(
            "document_id", "source_revision", "parse_revision", name="uq_document_parse_revision"
        ),
        sa.UniqueConstraint("job_id", name="uq_document_parse_job"),
        sa.UniqueConstraint("tenant_id", "project_id", "document_id", "id", name="uq_document_parse_scope"),
        sa.ForeignKeyConstraint(
            ["tenant_id", "project_id"],
            ["projects.tenant_id", "projects.id"],
            name="fk_document_parse_project_scope",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "project_id", "document_id"],
            ["documents.tenant_id", "documents.project_id", "documents.id"],
            name="fk_document_parse_document_scope",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "project_id", "job_id"],
            ["jobs.tenant_id", "jobs.project_id", "jobs.id"],
            name="fk_document_parse_job_scope",
        ),
        sa.CheckConstraint("source_revision >= 1", name="ck_document_parse_source_revision"),
        sa.CheckConstraint("parse_revision >= 0", name="ck_document_parse_parse_revision"),
        sa.CheckConstraint("engine IN ('basic', 'docling', 'legacy')", name="ck_document_parse_engine"),
        sa.CheckConstraint("origin IN ('worker', 'legacy')", name="ck_document_parse_origin"),
    )
    for column in ("tenant_id", "project_id"):
        op.create_index(f"ix_document_parse_versions_{column}", "document_parse_versions", [column])
    if op.get_bind().dialect.name == "postgresql":
        op.execute('ALTER TABLE "document_parse_versions" ENABLE ROW LEVEL SECURITY')
        op.execute('ALTER TABLE "document_parse_versions" FORCE ROW LEVEL SECURITY')
        condition = "tenant_id = nullif(current_setting('app.tenant_id', true), '')"
        op.execute(
            f'CREATE POLICY tenant_isolation ON "document_parse_versions" USING ({condition}) WITH CHECK ({condition})'
        )
        op.execute(
            'CREATE TRIGGER immutable_history BEFORE UPDATE OR DELETE ON "document_parse_versions" '
            "FOR EACH ROW EXECUTE FUNCTION reject_history_mutation()"
        )
    # An empty history table is deliberate. Legacy capture must validate the
    # exact retained IR and provenance inside the service's document lock.


def downgrade():
    raise RuntimeError("Document parse evidence requires a reviewed migration; downgrade is unsupported")
