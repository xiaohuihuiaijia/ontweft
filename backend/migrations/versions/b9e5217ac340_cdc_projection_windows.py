"""Frozen CDC bindings and atomic, auditable projection windows."""

import sqlalchemy as sa
from alembic import op

revision = "b9e5217ac340"
down_revision = "8c4f2d9a6b13"
branch_labels = None
depends_on = None


def common():
    return [
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("tenant_id", sa.String(36), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("project_id", sa.String(36), sa.ForeignKey("projects.id"), nullable=False),
        sa.Column("created_at", sa.String(40), nullable=False),
        sa.Column("stream_id", sa.String(36), sa.ForeignKey("cdc_streams.id"), nullable=False),
    ]


def binding_scope(name):
    return sa.ForeignKeyConstraint(
        ["tenant_id", "project_id", "stream_id", "binding_id"],
        [
            "cdc_projection_bindings.tenant_id",
            "cdc_projection_bindings.project_id",
            "cdc_projection_bindings.stream_id",
            "cdc_projection_bindings.id",
        ],
        name=name,
    )


def upgrade():
    # Fail before DDL if legacy streams already point outside their declared
    # tenant/project; never rewrite source identity or historic provenance.
    invalid = (
        op.get_bind()
        .execute(
            sa.text(
                "SELECT 1 FROM cdc_streams c LEFT JOIN sources s ON s.id = c.source_id "
                "WHERE s.id IS NULL OR c.tenant_id <> s.tenant_id OR c.project_id <> s.project_id LIMIT 1"
            )
        )
        .first()
    )
    if invalid:
        raise RuntimeError(
            "CDC stream/source scope is inconsistent; reviewed repair is required before migration"
        )
    with op.batch_alter_table("sources") as batch:
        batch.create_unique_constraint("uq_source_scope", ["tenant_id", "project_id", "id"])
    with op.batch_alter_table("releases") as batch:
        batch.create_unique_constraint("uq_release_scope", ["tenant_id", "project_id", "id"])
    with op.batch_alter_table("cdc_streams") as batch:
        batch.create_unique_constraint("uq_cdc_stream_scope", ["tenant_id", "project_id", "id"])
        batch.create_unique_constraint(
            "uq_cdc_stream_binding_scope",
            ["tenant_id", "project_id", "id", "source_id", "source_revision", "generation"],
        )
        batch.create_foreign_key(
            "fk_cdc_stream_source_scope",
            "sources",
            ["tenant_id", "project_id", "source_id"],
            ["tenant_id", "project_id", "id"],
        )
    op.create_table(
        "cdc_projection_bindings",
        *common(),
        sa.Column("source_id", sa.String(36), sa.ForeignKey("sources.id"), nullable=False),
        sa.Column("source_revision", sa.Integer(), nullable=False),
        sa.Column("generation", sa.String(36), nullable=False),
        sa.Column("release_id", sa.String(36), sa.ForeignKey("releases.id"), nullable=False),
        sa.Column("resources", sa.JSON(), nullable=False),
        sa.Column("binding_hash", sa.String(64), nullable=False),
        sa.Column("owner_id", sa.String(36), sa.ForeignKey("users.id"), nullable=False),
        sa.UniqueConstraint("stream_id"),
        sa.UniqueConstraint("tenant_id", "project_id", "stream_id", "id", name="uq_cdc_binding_scope"),
        sa.CheckConstraint("source_revision >= 1", name="ck_cdc_binding_revision"),
        sa.ForeignKeyConstraint(
            ["tenant_id", "project_id", "stream_id", "source_id", "source_revision", "generation"],
            [
                "cdc_streams.tenant_id",
                "cdc_streams.project_id",
                "cdc_streams.id",
                "cdc_streams.source_id",
                "cdc_streams.source_revision",
                "cdc_streams.generation",
            ],
            name="fk_cdc_binding_stream_scope",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "project_id", "release_id"],
            ["releases.tenant_id", "releases.project_id", "releases.id"],
            name="fk_cdc_binding_release_scope",
        ),
    )
    op.create_table(
        "cdc_projection_windows",
        *common(),
        sa.Column("binding_id", sa.String(36), sa.ForeignKey("cdc_projection_bindings.id"), nullable=False),
        sa.Column("start_sequence", sa.BigInteger(), nullable=False),
        sa.Column("end_sequence", sa.BigInteger(), nullable=False),
        sa.Column("kind", sa.String(30), nullable=False),
        sa.Column("proof", sa.JSON(none_as_null=True), nullable=True),
        sa.Column("schema_fingerprints", sa.JSON(), nullable=False),
        sa.Column("result", sa.JSON(), nullable=False),
        sa.Column("entity_ids", sa.JSON(), nullable=False),
        sa.Column("source_position", sa.JSON(), nullable=False),
        sa.UniqueConstraint("stream_id", "end_sequence"),
        sa.UniqueConstraint(
            "tenant_id", "project_id", "stream_id", "binding_id", "id", name="uq_cdc_window_scope"
        ),
        binding_scope("fk_cdc_window_binding_scope"),
        sa.CheckConstraint(
            "start_sequence >= 1 AND end_sequence >= start_sequence", name="ck_cdc_window_range"
        ),
    )
    # Window first: PostgreSQL validates the referenced relation immediately;
    # SQLite can also create this FK directly without rebuilding a new table.
    op.create_table(
        "cdc_projection_states",
        *common(),
        sa.Column("binding_id", sa.String(36), sa.ForeignKey("cdc_projection_bindings.id"), nullable=False),
        sa.Column("status", sa.String(30), nullable=False),
        sa.Column("reason_code", sa.String(100), nullable=False),
        sa.Column("message", sa.Text(), nullable=False),
        sa.Column("observed_sequence", sa.BigInteger(), nullable=False),
        sa.Column("schema_fingerprints", sa.JSON(), nullable=False),
        sa.Column("baseline_window_id", sa.String(36), nullable=True),
        sa.Column("last_job_id", sa.String(36), sa.ForeignKey("jobs.id"), nullable=True),
        sa.UniqueConstraint("stream_id"),
        sa.UniqueConstraint("binding_id"),
        binding_scope("fk_cdc_state_binding_scope"),
        sa.ForeignKeyConstraint(
            ["tenant_id", "project_id", "stream_id", "binding_id", "baseline_window_id"],
            [
                "cdc_projection_windows.tenant_id",
                "cdc_projection_windows.project_id",
                "cdc_projection_windows.stream_id",
                "cdc_projection_windows.binding_id",
                "cdc_projection_windows.id",
            ],
            name="fk_cdc_state_baseline_scope",
        ),
        sa.CheckConstraint("observed_sequence >= 0", name="ck_cdc_state_sequence"),
    )
    postgres = op.get_bind().dialect.name == "postgresql"
    for table in ("cdc_projection_bindings", "cdc_projection_states", "cdc_projection_windows"):
        for column in ("tenant_id", "project_id"):
            op.create_index(f"ix_{table}_{column}", table, [column])
        if postgres:
            op.execute(f'ALTER TABLE "{table}" ENABLE ROW LEVEL SECURITY')
            op.execute(f'ALTER TABLE "{table}" FORCE ROW LEVEL SECURITY')
            condition = "tenant_id = nullif(current_setting('app.tenant_id', true), '')"
            op.execute(
                f'CREATE POLICY tenant_isolation ON "{table}" USING ({condition}) WITH CHECK ({condition})'
            )
            if table != "cdc_projection_states":
                op.execute(
                    f'CREATE TRIGGER immutable_history BEFORE UPDATE OR DELETE ON "{table}" FOR EACH ROW EXECUTE FUNCTION reject_history_mutation()'
                )


def downgrade():
    raise RuntimeError(
        "Committed CDC projection history requires a reviewed migration; downgrade is unsupported"
    )
