"""Immutable upload versions; existing upload paths and evidence remain valid."""

import json
from uuid import uuid4

import sqlalchemy as sa
from alembic import op

revision = "ef311029a200"
down_revision = "ef311029a100"
branch_labels = None
depends_on = None


def upgrade():
    table = op.create_table(
        "source_file_versions",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("tenant_id", sa.String(36), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("project_id", sa.String(36), sa.ForeignKey("projects.id"), nullable=False),
        sa.Column("created_at", sa.String(40), nullable=False),
        sa.Column("source_id", sa.String(36), nullable=False),
        sa.Column("number", sa.Integer(), nullable=False),
        sa.Column("base_revision", sa.Integer(), nullable=False),
        sa.Column("applied_revision", sa.Integer(), nullable=True),
        sa.Column("config", sa.JSON(), nullable=False),
        sa.Column("resources", sa.JSON(), nullable=False),
        sa.Column("preview", sa.JSON(), nullable=False),
        sa.Column("jobs", sa.JSON(), nullable=False),
        sa.UniqueConstraint("source_id", "number", name="uq_source_file_number"),
        sa.ForeignKeyConstraint(
            ["tenant_id", "project_id", "source_id"],
            ["sources.tenant_id", "sources.project_id", "sources.id"],
            name="fk_file_version_source",
        ),
    )
    for field in ("tenant_id", "project_id", "source_id"):
        op.create_index("ix_source_file_versions_" + field, "source_file_versions", [field])
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.execute("ALTER TABLE sources NO FORCE ROW LEVEL SECURITY")
    # Owner migration reads the legacy immutable upload metadata, never bytes.
    for row in bind.execute(
        sa.text(
            "SELECT id,tenant_id,project_id,created_at,revision,config,resources FROM sources WHERE kind='tabular'"
        )
    ).mappings():
        decode = lambda value: json.loads(value) if isinstance(value, str) else value
        bind.execute(
            table.insert().values(
                id=str(uuid4()),
                tenant_id=row["tenant_id"],
                project_id=row["project_id"],
                created_at=row["created_at"],
                source_id=row["id"],
                number=1,
                base_revision=row["revision"],
                applied_revision=row["revision"],
                config=decode(row["config"]),
                resources=decode(row["resources"]),
                preview={},
                jobs=[],
            )
        )
    if bind.dialect.name == "postgresql":
        op.execute("ALTER TABLE sources FORCE ROW LEVEL SECURITY")
        op.execute("ALTER TABLE source_file_versions ENABLE ROW LEVEL SECURITY")
        op.execute("ALTER TABLE source_file_versions FORCE ROW LEVEL SECURITY")
        condition = "tenant_id = nullif(current_setting('app.tenant_id', true), '')"
        op.execute(
            f"CREATE POLICY tenant_isolation ON source_file_versions USING ({condition}) WITH CHECK ({condition})"
        )


def downgrade():
    raise RuntimeError("File version rollback requires preserving retained upload versions")
