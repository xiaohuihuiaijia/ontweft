"""Exact-key relation rules, contribution ownership and transactional work."""

import sqlalchemy as sa
from alembic import op

revision = "ef311029a300"
down_revision = "ef311029a200"
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
    op.add_column("relations", sa.Column("manual", sa.Boolean(), nullable=False, server_default=sa.true()))
    op.create_table(
        "relation_rules",
        *common(),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("config", sa.JSON(), nullable=False),
        sa.Column("owner_id", sa.String(36), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("cursor", sa.String(36), nullable=False),
        sa.Column("indexed", sa.Boolean(), nullable=False),
        sa.Column("job_id", sa.String(36), sa.ForeignKey("jobs.id"), nullable=True),
        sa.Column("error", sa.String(500), nullable=True),
    )
    op.create_table(
        "relation_match_index",
        *common(),
        sa.Column("rule_id", sa.String(36), sa.ForeignKey("relation_rules.id"), nullable=False),
        sa.Column("side", sa.String(6), nullable=False),
        sa.Column("entity_id", sa.String(36), sa.ForeignKey("entities.id"), nullable=False),
        sa.Column("value_hash", sa.String(64), nullable=True),
        sa.UniqueConstraint("rule_id", "side", "entity_id", name="uq_rule_match_entity"),
    )
    op.create_index(
        "ix_rule_match_value", "relation_match_index", ["rule_id", "side", "value_hash", "entity_id"]
    )
    op.create_table(
        "relation_work",
        *common(),
        sa.Column("rule_id", sa.String(36), sa.ForeignKey("relation_rules.id"), nullable=False),
        sa.Column("entity_id", sa.String(36), sa.ForeignKey("entities.id"), nullable=False),
        sa.Column("status", sa.String(12), nullable=False),
        sa.Column("error", sa.String(300), nullable=True),
        sa.UniqueConstraint("rule_id", "entity_id", name="uq_rule_work_entity"),
    )
    op.create_index("ix_rule_work_pending", "relation_work", ["rule_id", "status", "id"])
    op.create_table(
        "relation_contributions",
        *common(),
        sa.Column("rule_id", sa.String(36), sa.ForeignKey("relation_rules.id"), nullable=False),
        sa.Column("source_id", sa.String(36), sa.ForeignKey("entities.id"), nullable=False),
        sa.Column("relation_id", sa.String(36), sa.ForeignKey("relations.id"), nullable=False),
        sa.UniqueConstraint("rule_id", "source_id", name="uq_rule_contribution_source"),
    )
    for field in ("rule_id", "relation_id"):
        op.create_index("ix_relation_contributions_" + field, "relation_contributions", [field])
    for table in ("relation_rules", "relation_match_index", "relation_work", "relation_contributions"):
        for field in ("tenant_id", "project_id"):
            op.create_index("ix_" + table + "_" + field, table, [field])
        if op.get_bind().dialect.name == "postgresql":
            op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
            op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
            condition = "tenant_id = nullif(current_setting('app.tenant_id', true), '')"
            op.execute(
                f"CREATE POLICY tenant_isolation ON {table} USING ({condition}) WITH CHECK ({condition})"
            )


def downgrade():
    raise RuntimeError("Rule rollback requires preserving manual and rule-owned relationship contributions")
