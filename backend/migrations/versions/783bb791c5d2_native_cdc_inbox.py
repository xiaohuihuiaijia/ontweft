"""Native event receipt, scoped collection credentials and independent watermarks."""
from alembic import op
import sqlalchemy as sa

revision = "783bb791c5d2"
down_revision = "cc56c18b72ef"
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
    op.create_table("cdc_streams", *common(),
        sa.Column("source_id", sa.String(36), sa.ForeignKey("sources.id"), nullable=False),
        sa.Column("source_revision", sa.Integer(), nullable=False),
        sa.Column("generation", sa.String(36), nullable=False),
        sa.Column("connector", sa.String(30), nullable=False),
        sa.Column("owner_id", sa.String(36), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("token_hash", sa.String(64), nullable=False),
        sa.Column("state", sa.String(30), nullable=False),
        sa.Column("received_sequence", sa.BigInteger(), nullable=False),
        sa.Column("projected_sequence", sa.BigInteger(), nullable=False),
        sa.Column("indexed_sequence", sa.BigInteger(), nullable=False),
        sa.Column("source_partition", sa.JSON(), nullable=True),
        sa.Column("lease_holder", sa.String(36), nullable=True),
        sa.Column("lease_until", sa.Float(), nullable=False),
        sa.Column("fencing_token", sa.Integer(), nullable=False),
        sa.UniqueConstraint("source_id"),
    )
    op.create_table("cdc_events", *common(),
        sa.Column("stream_id", sa.String(36), sa.ForeignKey("cdc_streams.id"), nullable=False),
        sa.Column("sequence", sa.BigInteger(), nullable=False),
        sa.Column("event_id", sa.String(64), nullable=False),
        sa.Column("record_hash", sa.String(64), nullable=False),
        sa.Column("record", sa.JSON(), nullable=False),
        sa.UniqueConstraint("stream_id", "event_id"),
        sa.UniqueConstraint("stream_id", "sequence"),
    )
    for table in ("cdc_streams", "cdc_events"):
        for field in ("tenant_id", "project_id"):
            op.create_index(f"ix_{table}_{field}", table, [field])
        if op.get_bind().dialect.name == "postgresql":
            op.execute(f'ALTER TABLE "{table}" ENABLE ROW LEVEL SECURITY')
            op.execute(f'ALTER TABLE "{table}" FORCE ROW LEVEL SECURITY')
            condition = "tenant_id = nullif(current_setting('app.tenant_id', true), '')"
            op.execute(f'CREATE POLICY tenant_isolation ON "{table}" USING ({condition}) WITH CHECK ({condition})')
    op.create_index("ix_cdc_events_stream_id", "cdc_events", ["stream_id"])
    if op.get_bind().dialect.name == "postgresql":
        op.execute('CREATE TRIGGER immutable_history BEFORE UPDATE OR DELETE ON cdc_events FOR EACH ROW EXECUTE FUNCTION reject_history_mutation()')


def downgrade():
    op.drop_table("cdc_events")
    op.drop_table("cdc_streams")
