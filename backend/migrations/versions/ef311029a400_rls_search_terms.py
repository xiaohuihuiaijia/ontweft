"""Use indexed equality for normalized terms under forced tenant isolation.

Only rebuildable object-search projections are invalidated. Objects, evidence,
published models and document-search semantics are unchanged.
"""

from alembic import op

revision = "ef311029a400"
down_revision = "ef311029a300"
branch_labels = None
depends_on = None


def upgrade():
    pg = op.get_bind().dialect.name == "postgresql"
    if pg:
        for table in ("object_search_fields", "search_project_states"):
            op.execute(f"ALTER TABLE {table} NO FORCE ROW LEVEL SECURITY")
        op.drop_index("ix_object_search_fields_fts", table_name="object_search_fields")
    # Old rows contain chunks, not individual terms. Advertise the rebuild and
    # let the existing bounded worker populate the new derived representation.
    op.execute("DELETE FROM object_search_fields")
    op.execute("UPDATE search_project_states SET backfill_cursor='', backfill_complete=false, error=NULL")
    op.create_index(
        "ix_object_search_fields_term",
        "object_search_fields",
        ["tenant_id", "project_id", "generation", "token_text", "field_key", "entity_id"],
    )
    if pg:
        for table in ("object_search_fields", "search_project_states"):
            op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")


def downgrade():
    raise RuntimeError("Restore the previous application and rebuild its derived search projection")
