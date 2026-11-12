"""Retain historical CDC streams while admitting one current generation per source."""

import sqlalchemy as sa
from alembic import op

revision = "ef311029a600"
down_revision = "ef311029a500"
branch_labels = None
depends_on = None


def upgrade():
    constraints = sa.inspect(op.get_bind()).get_unique_constraints("cdc_streams")
    old = next(c for c in constraints if c["column_names"] == ["source_id"])
    with op.batch_alter_table(
        "cdc_streams", naming_convention={"uq": "uq_%(table_name)s_%(column_0_name)s"}
    ) as batch:
        batch.drop_constraint(old["name"] or "uq_cdc_streams_source_id", type_="unique")
        batch.add_column(sa.Column("previous_stream_id", sa.String(36), nullable=True))
        batch.create_foreign_key("fk_cdc_previous_stream", "cdc_streams", ["previous_stream_id"], ["id"])
        batch.create_unique_constraint("uq_cdc_previous_stream", ["previous_stream_id"])
    op.create_index(
        "uq_cdc_current_source",
        "cdc_streams",
        ["source_id"],
        unique=True,
        postgresql_where=sa.text("state <> 'retired'"),
        sqlite_where=sa.text("state <> 'retired'"),
    )


def downgrade():
    raise RuntimeError("Historical CDC generations must be retained; restore a reviewed backup to downgrade")
