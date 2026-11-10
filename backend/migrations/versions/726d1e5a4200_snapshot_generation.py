"""Monotonic capture generations prevent stale projection commits."""
from alembic import op
import sqlalchemy as sa

revision = "726d1e5a4200"
down_revision = "1ecf7faa3a25"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("source_snapshots", sa.Column("generation", sa.Integer(), nullable=True))
    op.execute("""WITH numbered AS (SELECT id, ROW_NUMBER() OVER
        (PARTITION BY source_id, resource_id ORDER BY created_at, id) AS ordinal FROM source_snapshots)
        UPDATE source_snapshots SET generation = (SELECT ordinal FROM numbered WHERE numbered.id = source_snapshots.id)""")
    with op.batch_alter_table("source_snapshots") as batch:
        batch.alter_column("generation", existing_type=sa.Integer(), nullable=False)
        batch.create_unique_constraint("uq_snapshot_generation", ["source_id", "resource_id", "generation"])


def downgrade():
    with op.batch_alter_table("source_snapshots") as batch:
        batch.drop_constraint("uq_snapshot_generation", type_="unique")
        batch.drop_column("generation")
