"""Append-only contribution decisions, without inventing past human reviews."""

import sqlalchemy as sa
from alembic import op

revision = "a7d83e2c190f"
down_revision = "f2c7d9a41b60"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "contribution_decisions",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("tenant_id", sa.String(36), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("project_id", sa.String(36), sa.ForeignKey("projects.id"), nullable=False),
        sa.Column("created_at", sa.String(40), nullable=False),
        sa.Column("entity_id", sa.String(36), nullable=False),
        sa.Column("field_key", sa.String(80), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("operation", sa.String(20), nullable=False),
        sa.Column("selected_contribution_id", sa.String(36), nullable=True),
        sa.Column("selected_contribution_revision", sa.Integer(), nullable=True),
        sa.Column("model_release_id", sa.String(36), nullable=False),
        sa.Column("entity_revision", sa.Integer(), nullable=False),
        sa.Column("basis", sa.JSON(none_as_null=True), nullable=False),
        sa.Column("basis_fingerprint", sa.String(64), nullable=False),
        sa.Column("preview_fingerprint", sa.String(64), nullable=False),
        sa.Column("reason", sa.String(2000), nullable=False),
        sa.Column("actor_id", sa.String(36), sa.ForeignKey("users.id"), nullable=False),
        sa.UniqueConstraint("entity_id", "field_key", "revision", name="uq_contribution_decision_revision"),
        sa.UniqueConstraint("tenant_id", "project_id", "id", name="uq_contribution_decision_scope"),
        sa.ForeignKeyConstraint(
            ["tenant_id", "project_id", "entity_id"],
            ["entities.tenant_id", "entities.project_id", "entities.id"],
            name="fk_contribution_decision_entity_scope",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "project_id", "selected_contribution_id"],
            ["source_contributions.tenant_id", "source_contributions.project_id", "source_contributions.id"],
            name="fk_contribution_decision_source_scope",
        ),
        sa.ForeignKeyConstraint(
            ["selected_contribution_id", "selected_contribution_revision"],
            ["contribution_revisions.contribution_id", "contribution_revisions.revision"],
            name="fk_contribution_decision_source_revision",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "project_id", "model_release_id"],
            ["releases.tenant_id", "releases.project_id", "releases.id"],
            name="fk_contribution_decision_release_scope",
        ),
        sa.CheckConstraint("revision >= 1", name="ck_contribution_decision_revision"),
        sa.CheckConstraint("entity_revision >= 1", name="ck_contribution_decision_entity_revision"),
        sa.CheckConstraint(
            "operation IN ('select', 'hold', 'clear')", name="ck_contribution_decision_operation"
        ),
        sa.CheckConstraint(
            "(operation = 'select' AND selected_contribution_id IS NOT NULL "
            "AND selected_contribution_revision IS NOT NULL AND selected_contribution_revision >= 1) OR "
            "(operation IN ('hold', 'clear') AND selected_contribution_id IS NULL "
            "AND selected_contribution_revision IS NULL)",
            name="ck_contribution_decision_selection",
        ),
        sa.CheckConstraint("CAST(basis AS TEXT) <> 'null'", name="ck_contribution_decision_basis_not_null"),
        sa.CheckConstraint(
            "length(reason) <= 2000 AND "
            "length(trim(replace(replace(replace(reason, '\t', ''), '\n', ''), '\r', ''))) > 0",
            name="ck_contribution_decision_reason",
        ),
        sa.CheckConstraint("length(field_key) BETWEEN 1 AND 80", name="ck_contribution_decision_field"),
        sa.CheckConstraint("length(basis_fingerprint) = 64", name="ck_contribution_decision_basis_hash"),
        sa.CheckConstraint("length(preview_fingerprint) = 64", name="ck_contribution_decision_preview_hash"),
    )
    for column in ("tenant_id", "project_id", "entity_id"):
        op.create_index(f"ix_contribution_decisions_{column}", "contribution_decisions", [column])
    if op.get_bind().dialect.name == "postgresql":
        op.execute('ALTER TABLE "contribution_decisions" ENABLE ROW LEVEL SECURITY')
        op.execute('ALTER TABLE "contribution_decisions" FORCE ROW LEVEL SECURITY')
        condition = "tenant_id = nullif(current_setting('app.tenant_id', true), '')"
        op.execute(
            f'CREATE POLICY tenant_isolation ON "contribution_decisions" USING ({condition}) WITH CHECK ({condition})'
        )
        op.execute(
            'CREATE TRIGGER immutable_history BEFORE UPDATE OR DELETE ON "contribution_decisions" '
            "FOR EACH ROW EXECUTE FUNCTION reject_history_mutation()"
        )


def downgrade():
    raise RuntimeError("Contribution decisions require a reviewed migration; downgrade is unsupported")
