"""Versioned function governance and immutable calculation receipts."""
from alembic import op
import sqlalchemy as sa

revision = "cc56c18b72ef"
down_revision = "e83a7bc41c66"
branch_labels = None
depends_on = None


def common():
    return [
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("tenant_id", sa.String(36), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("project_id", sa.String(36), sa.ForeignKey("projects.id"), nullable=False),
        sa.Column("created_at", sa.String(40), nullable=False),
    ]


def upgrade():
    op.create_table("function_versions", *common(),
        sa.Column("key", sa.String(80), nullable=False),
        sa.Column("label", sa.String(200), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(30), nullable=False),
        sa.Column("kernel_key", sa.String(80), nullable=False),
        sa.Column("kernel_version", sa.Integer(), nullable=False),
        sa.Column("bindings", sa.JSON(), nullable=False),
        sa.Column("unit", sa.String(80), nullable=False),
        sa.Column("baseline_release_id", sa.String(36), sa.ForeignKey("releases.id"), nullable=False),
        sa.Column("artifact", sa.JSON(), nullable=False),
        sa.Column("snapshot_hash", sa.String(64), nullable=False),
        sa.Column("author_id", sa.String(36), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("reviewed_by", sa.String(36), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("reviewed_hash", sa.String(64), nullable=True),
        sa.Column("published_by", sa.String(36), sa.ForeignKey("users.id"), nullable=True),
        sa.UniqueConstraint("project_id", "key", "version"),
    )
    op.create_table("function_runs", *common(),
        sa.Column("function_version_id", sa.String(36), sa.ForeignKey("function_versions.id"), nullable=False),
        sa.Column("actor_id", sa.String(36), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("model_release_id", sa.String(36), sa.ForeignKey("releases.id"), nullable=False),
        sa.Column("request_hash", sa.String(64), nullable=False),
        sa.Column("artifact_hash", sa.String(64), nullable=False),
        sa.Column("inputs", sa.JSON(), nullable=False),
        sa.Column("result", sa.JSON(), nullable=False),
    )
    for table in ("function_versions", "function_runs"):
        for field in ("tenant_id", "project_id"):
            op.create_index(f"ix_{table}_{field}", table, [field])
        if op.get_bind().dialect.name == "postgresql":
            op.execute(f'ALTER TABLE "{table}" ENABLE ROW LEVEL SECURITY')
            op.execute(f'ALTER TABLE "{table}" FORCE ROW LEVEL SECURITY')
            condition = "tenant_id = nullif(current_setting('app.tenant_id', true), '')"
            op.execute(f'CREATE POLICY tenant_isolation ON "{table}" USING ({condition}) WITH CHECK ({condition})')
    op.create_index("ix_function_runs_function_version_id", "function_runs", ["function_version_id"])
    if op.get_bind().dialect.name == "postgresql":
        op.execute('CREATE TRIGGER immutable_history BEFORE UPDATE OR DELETE ON function_runs FOR EACH ROW EXECUTE FUNCTION reject_history_mutation()')


def downgrade():
    op.drop_table("function_runs")
    op.drop_table("function_versions")
