"""Add exact-origin CDC evidence references without decoded values or projection.

Existing snapshot evidence is neither re-encoded nor assigned a new identity.
Reference-only native events cannot become SourceIdentity.last_record_id.
"""

import sqlalchemy as sa
from alembic import op

revision = "d407be3a9128"
down_revision = "a928dc57be12"
branch_labels = None
depends_on = None

ORIGIN_CHECK = (
    "(record_codec = 'snapshot-json-v1' AND snapshot_id IS NOT NULL AND attempt IS NOT NULL "
    "AND attempt >= 1 AND cdc_event_id IS NULL AND key_hash IS NOT NULL "
    'AND source_key IS NOT NULL AND "values" IS NOT NULL AND content_hash IS NOT NULL) OR '
    "(record_codec = 'cdc-native-reference-v1' AND cdc_event_id IS NOT NULL "
    "AND snapshot_id IS NULL AND attempt IS NULL AND key_hash IS NULL AND source_key IS NULL "
    'AND identity_key IS NULL AND "values" IS NULL AND content_hash IS NULL)'
)


def validate_existing(bind, postgres):
    # An existing single-column FK does not prove tenant/project agreement.
    # Inspect every tenant under FORCE RLS rather than disabling its policy.
    for tenant in bind.execute(sa.text("SELECT id FROM tenants")).scalars().all():
        if postgres:
            bind.execute(sa.text("SELECT set_config('app.tenant_id', :tenant, true)"), {"tenant": tenant})
        invalid_record = bind.execute(
            sa.text("""
            SELECT r.id FROM source_record_revisions r
            LEFT JOIN source_snapshots s ON s.id = r.snapshot_id
              AND s.tenant_id = r.tenant_id AND s.project_id = r.project_id
            WHERE r.tenant_id = :tenant AND
              (s.id IS NULL OR r.attempt IS NULL OR r.attempt < 1 OR r.key_hash IS NULL
               OR r.source_key IS NULL OR r."values" IS NULL OR r.content_hash IS NULL)
            LIMIT 1
        """),
            {"tenant": tenant},
        ).first()
        invalid_identity = bind.execute(
            sa.text("""
            SELECT i.id FROM source_identities i
            LEFT JOIN source_record_revisions r ON r.id = i.last_record_id
              AND r.tenant_id = i.tenant_id AND r.project_id = i.project_id
            WHERE i.tenant_id = :tenant AND r.id IS NULL LIMIT 1
        """),
            {"tenant": tenant},
        ).first()
        if invalid_record or invalid_identity:
            raise RuntimeError(
                "Existing source evidence has an invalid origin or scope; review it before migration. "
                "No record or identity has been rewritten."
            )
    if postgres:
        bind.execute(sa.text("SELECT set_config('app.tenant_id', '', true)"))


def upgrade():
    bind = op.get_bind()
    postgres = bind.dialect.name == "postgresql"
    if postgres:
        op.execute(
            "LOCK TABLE source_snapshots, cdc_events, source_record_revisions, source_identities IN ACCESS EXCLUSIVE MODE"
        )
    # Preflight precedes all DDL, including on SQLite where DDL rollback differs.
    validate_existing(bind, postgres)
    with op.batch_alter_table("source_snapshots") as batch:
        batch.create_unique_constraint("uq_snapshot_scope", ["tenant_id", "project_id", "id"])
    with op.batch_alter_table("cdc_events") as batch:
        batch.create_unique_constraint("uq_cdc_event_scope", ["tenant_id", "project_id", "id"])
    with op.batch_alter_table("source_record_revisions") as batch:
        batch.add_column(
            sa.Column("record_codec", sa.String(40), nullable=False, server_default="snapshot-json-v1")
        )
        batch.add_column(sa.Column("cdc_event_id", sa.String(36), nullable=True))
        batch.alter_column("snapshot_id", existing_type=sa.String(36), nullable=True)
        batch.alter_column("attempt", existing_type=sa.Integer(), nullable=True)
        batch.alter_column("key_hash", existing_type=sa.String(64), nullable=True)
        batch.alter_column("source_key", existing_type=sa.JSON(), nullable=True)
        batch.alter_column("values", existing_type=sa.JSON(), nullable=True)
        batch.alter_column("content_hash", existing_type=sa.String(64), nullable=True)
        batch.create_unique_constraint("uq_source_record_cdc_event", ["cdc_event_id"])
        batch.create_unique_constraint(
            "uq_source_record_scope_codec", ["tenant_id", "project_id", "id", "record_codec"]
        )
        batch.create_foreign_key("fk_source_record_cdc_event", "cdc_events", ["cdc_event_id"], ["id"])
        batch.create_foreign_key(
            "fk_source_record_snapshot_scope",
            "source_snapshots",
            ["tenant_id", "project_id", "snapshot_id"],
            ["tenant_id", "project_id", "id"],
        )
        batch.create_foreign_key(
            "fk_source_record_cdc_scope",
            "cdc_events",
            ["tenant_id", "project_id", "cdc_event_id"],
            ["tenant_id", "project_id", "id"],
        )
        batch.create_check_constraint("ck_source_record_exact_origin", ORIGIN_CHECK)
    with op.batch_alter_table("source_identities") as batch:
        batch.add_column(
            sa.Column("last_record_codec", sa.String(40), nullable=False, server_default="snapshot-json-v1")
        )
        batch.create_check_constraint(
            "ck_identity_projectable_record", "last_record_codec = 'snapshot-json-v1'"
        )
        batch.create_foreign_key(
            "fk_identity_record_scope_codec",
            "source_record_revisions",
            ["tenant_id", "project_id", "last_record_id", "last_record_codec"],
            ["tenant_id", "project_id", "id", "record_codec"],
        )
    # PostgreSQL ALTER retains FORCE RLS and immutable_history. No history
    # trigger is disabled: old payloads, hashes, IDs and pointers stay intact.


def downgrade():
    raise RuntimeError(
        "Native evidence references require a reviewed migration; automatic downgrade is unsupported"
    )
