"""Source contribution ledger and non-destructive materialization status.

Legacy source payloads and identity pointers are not rewritten. Verified legacy
snapshot contributions are populated lazily before the caller changes pointers.
"""

import sqlalchemy as sa
from alembic import op

revision = "8c4f2d9a6b13"
down_revision = "d407be3a9128"
branch_labels = None
depends_on = None


def common():
    return [
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("tenant_id", sa.String(36), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("project_id", sa.String(36), sa.ForeignKey("projects.id"), nullable=False),
        sa.Column("created_at", sa.String(40), nullable=False),
        sa.Column("identity_id", sa.String(36), sa.ForeignKey("source_identities.id"), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("record_id", sa.String(36), sa.ForeignKey("source_record_revisions.id"), nullable=False),
        sa.Column("record_codec", sa.String(40), nullable=False),
        sa.Column("release_id", sa.String(36), sa.ForeignKey("releases.id"), nullable=False),
        sa.Column("mapping_key", sa.String(80), nullable=False),
        sa.Column("mapped_values", sa.JSON(), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False),
        sa.Column("withdraw_reason", sa.String(500), nullable=True),
        sa.Column("native_row", sa.JSON(none_as_null=True), nullable=True),
        sa.Column("stream_id", sa.String(36), sa.ForeignKey("cdc_streams.id"), nullable=True),
        sa.Column("sequence", sa.BigInteger(), nullable=True),
    ]


def constraints(prefix):
    return [
        sa.ForeignKeyConstraint(
            ["tenant_id", "project_id", "identity_id"],
            ["source_identities.tenant_id", "source_identities.project_id", "source_identities.id"],
            name=f"fk_{prefix}_identity_scope",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "project_id", "record_id", "record_codec"],
            ["source_record_revisions.tenant_id", "source_record_revisions.project_id", "source_record_revisions.id", "source_record_revisions.record_codec"],
            name=f"fk_{prefix}_record_scope",
        ),
        sa.CheckConstraint("revision >= 1", name=f"ck_{prefix}_revision"),
        sa.CheckConstraint(
            "(active AND withdraw_reason IS NULL) OR (NOT active AND withdraw_reason IS NOT NULL AND length(withdraw_reason) > 0)",
            name=f"ck_{prefix}_withdrawal",
        ),
        sa.CheckConstraint(
            "(record_codec = 'snapshot-json-v1' AND stream_id IS NULL AND sequence IS NULL AND native_row IS NULL) OR "
            "(record_codec = 'cdc-native-reference-v1' AND stream_id IS NOT NULL AND sequence IS NOT NULL AND sequence >= 1)",
            name=f"ck_{prefix}_origin",
        ),
    ]


def upgrade():
    postgres = op.get_bind().dialect.name == "postgresql"
    if postgres:
        op.execute("LOCK TABLE entities, entity_revisions, source_identities IN ACCESS EXCLUSIVE MODE")
    with op.batch_alter_table("entities") as batch:
        batch.add_column(sa.Column("data_status", sa.String(20), nullable=False, server_default="active"))
        batch.create_check_constraint("ck_entity_data_status", "data_status IN ('active', 'conflict', 'incomplete', 'withdrawn')")
    with op.batch_alter_table("entity_revisions") as batch:
        batch.add_column(sa.Column("data_status", sa.String(20), nullable=False, server_default="active"))
    with op.batch_alter_table("source_identities") as batch:
        batch.create_unique_constraint("uq_source_identity_scope", ["tenant_id", "project_id", "id"])
        batch.drop_constraint("ck_identity_projectable_record", type_="check")
        batch.create_check_constraint("ck_identity_projectable_record", "last_record_codec IN ('snapshot-json-v1', 'cdc-native-reference-v1')")
    op.create_table("source_contributions", *common(),
        sa.UniqueConstraint("identity_id", name="uq_contribution_identity"),
        sa.UniqueConstraint("tenant_id", "project_id", "id", name="uq_contribution_scope"),
        *constraints("contribution"),
    )
    op.create_table("contribution_revisions", *common(),
        sa.Column("contribution_id", sa.String(36), sa.ForeignKey("source_contributions.id"), nullable=False),
        sa.Column("actor_id", sa.String(36), sa.ForeignKey("users.id"), nullable=False),
        sa.UniqueConstraint("contribution_id", "revision", name="uq_contribution_revision"),
        sa.ForeignKeyConstraint(
            ["tenant_id", "project_id", "contribution_id"],
            ["source_contributions.tenant_id", "source_contributions.project_id", "source_contributions.id"],
            name="fk_contribution_revision_scope",
        ),
        *constraints("contribution_revision"),
    )
    for table in ("source_contributions", "contribution_revisions"):
        for column in ("tenant_id", "project_id", "identity_id"):
            op.create_index(f"ix_{table}_{column}", table, [column])
        if postgres:
            op.execute(f'ALTER TABLE "{table}" ENABLE ROW LEVEL SECURITY')
            op.execute(f'ALTER TABLE "{table}" FORCE ROW LEVEL SECURITY')
            condition = "tenant_id = nullif(current_setting('app.tenant_id', true), '')"
            op.execute(f'CREATE POLICY tenant_isolation ON "{table}" USING ({condition}) WITH CHECK ({condition})')
    op.create_index("ix_contribution_revisions_contribution_id", "contribution_revisions", ["contribution_id"])
    if postgres:
        op.execute("CREATE TRIGGER immutable_history BEFORE UPDATE OR DELETE ON contribution_revisions FOR EACH ROW EXECUTE FUNCTION reject_history_mutation()")


def downgrade():
    raise RuntimeError("Contribution withdrawal/history requires reviewed migration; automatic downgrade is unsupported")
