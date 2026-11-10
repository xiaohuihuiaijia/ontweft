"""Tenant isolation, scoped sessions and immutable history.

Revision ID: caa24ef6b910
Revises: 642792401804
"""
from alembic import op
import sqlalchemy as sa

revision = "caa24ef6b910"
down_revision = "642792401804"
branch_labels = None
depends_on = None

TABLES = (
    "projects", "users", "sessions", "memberships", "changesets", "releases", "entities",
    "entity_revisions", "relations", "sources", "documents", "knowledge_pages",
    "knowledge_revisions", "jobs", "audit_events", "action_runs",
)
IMMUTABLE = ("releases", "entity_revisions", "knowledge_revisions", "audit_events")


def upgrade():
    with op.batch_alter_table("sessions") as batch:
        batch.add_column(sa.Column("tenant_id", sa.String(36), nullable=True))
    op.execute("UPDATE sessions SET tenant_id = (SELECT tenant_id FROM users WHERE users.id = sessions.user_id)")
    with op.batch_alter_table("sessions") as batch:
        batch.alter_column("tenant_id", existing_type=sa.String(36), nullable=False)
        batch.create_foreign_key("fk_sessions_tenant", "tenants", ["tenant_id"], ["id"])
        batch.create_index("ix_sessions_tenant_id", ["tenant_id"])
    if op.get_bind().dialect.name != "postgresql":
        return
    for table in TABLES:
        op.execute(f'ALTER TABLE "{table}" ENABLE ROW LEVEL SECURITY')
        op.execute(f'ALTER TABLE "{table}" FORCE ROW LEVEL SECURITY')
        condition = "tenant_id = nullif(current_setting('app.tenant_id', true), '')"
        op.execute(f'CREATE POLICY tenant_isolation ON "{table}" USING ({condition}) WITH CHECK ({condition})')
    # Resolving an opaque cookie reveals only its own session, before tenant context is known.
    op.execute("CREATE POLICY session_resolution ON sessions FOR SELECT USING (token_hash = nullif(current_setting('app.session_hash', true), ''))")
    op.execute("""CREATE FUNCTION reject_history_mutation() RETURNS trigger
        LANGUAGE plpgsql AS $$ BEGIN
        RAISE EXCEPTION 'Immutable history cannot be updated or deleted' USING ERRCODE = '42501';
        END $$""")
    for table in IMMUTABLE:
        op.execute(f'CREATE TRIGGER immutable_history BEFORE UPDATE OR DELETE ON "{table}" FOR EACH ROW EXECUTE FUNCTION reject_history_mutation()')


def downgrade():
    if op.get_bind().dialect.name == "postgresql":
        for table in IMMUTABLE:
            op.execute(f'DROP TRIGGER immutable_history ON "{table}"')
        op.execute("DROP FUNCTION reject_history_mutation()")
        op.execute("DROP POLICY session_resolution ON sessions")
        for table in TABLES:
            op.execute(f'DROP POLICY tenant_isolation ON "{table}"')
            op.execute(f'ALTER TABLE "{table}" DISABLE ROW LEVEL SECURITY')
            op.execute(f'ALTER TABLE "{table}" NO FORCE ROW LEVEL SECURITY')
    with op.batch_alter_table("sessions") as batch:
        batch.drop_index("ix_sessions_tenant_id")
        batch.drop_constraint("fk_sessions_tenant", type_="foreignkey")
        batch.drop_column("tenant_id")
