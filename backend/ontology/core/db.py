from contextlib import contextmanager

from sqlalchemy import create_engine, event, text
from sqlalchemy.orm import DeclarativeBase, sessionmaker

from ontology.core.config import Settings


class Base(DeclarativeBase):
    pass


class Database:
    def __init__(self, settings: Settings):
        from ontology.core.registry import register_models

        register_models()
        settings.check()
        settings.data_dir.mkdir(parents=True, exist_ok=True)
        options = (
            {"connect_args": {"check_same_thread": False, "timeout": 15}}
            if settings.database_url.startswith("sqlite:")
            else {"pool_pre_ping": True}
        )
        self.engine = create_engine(settings.database_url, **options)
        if self.engine.dialect.name == "sqlite":

            @event.listens_for(self.engine, "connect")
            def configure_sqlite(connection, _):
                connection.execute("PRAGMA foreign_keys=ON")
                connection.execute("PRAGMA journal_mode=WAL")

        self.sessions = sessionmaker(bind=self.engine, expire_on_commit=False)
        if settings.environment == "production":
            try:
                self.verify_runtime_role()
            except Exception:
                self.engine.dispose()
                raise

    def verify_runtime_role(self):

        with self.engine.connect() as connection:
            role = connection.execute(
                text("SELECT rolsuper, rolbypassrls FROM pg_roles WHERE rolname=current_user")
            ).one()
            if (
                role[0]
                or role[1]
                or connection.scalar(text("SELECT has_schema_privilege(current_user, 'public', 'CREATE')"))
            ):
                raise ValueError(
                    "Production must use a restricted runtime role, separate from the migration owner"
                )
            rows = connection.execute(
                text("""SELECT c.relname, c.relrowsecurity, c.relforcerowsecurity,
                pg_has_role(current_user, c.relowner, 'MEMBER') AS owns_table
                FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
                WHERE n.nspname='public' AND c.relkind='r'""")
            ).all()
            tables = {row[0]: row for row in rows}
            for name in Base.metadata.tables:
                row = tables.get(name)
                if row is None or row[3] or (name != "tenants" and not (row[1] and row[2])):
                    raise ValueError(
                        "Production requires current migrations, forced tenant policies, and no table-owner membership"
                    )

    def create_schema(self):

        Base.metadata.create_all(self.engine)

    @contextmanager
    def transaction(self, tenant_id: str | None = None, *, session_hash: str | None = None):
        with self.sessions.begin() as session:
            if self.engine.dialect.name == "postgresql":
                # Transaction-local settings also clear context on pooled connections.
                session.execute(
                    text(
                        "SELECT set_config('app.tenant_id', :tenant, true), set_config('app.session_hash', :token, true)"
                    ),
                    {"tenant": tenant_id or "", "token": session_hash or ""},
                )
            yield session
