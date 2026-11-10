"""Apply reviewed migrations and grant the fixed least-privilege runtime role."""

import os

from alembic import command
from alembic.config import Config
from ontology.core.config import Settings
from ontology.core.registry import register_models
from sqlalchemy import create_engine, literal, text


def main():
    password = os.environ.get("ONTOLOGY_RUNTIME_PASSWORD")
    if not password:
        raise SystemExit("Set ONTOLOGY_RUNTIME_PASSWORD before migration")
    command.upgrade(Config("alembic.ini"), "head")
    engine = create_engine(Settings().database_url)
    immutable = {
        "releases",
        "entity_revisions",
        "knowledge_revisions",
        "document_sources",
        "knowledge_source_dependencies",
        "document_parse_versions",
        "audit_events",
        "source_record_revisions",
        "action_attempts",
        "function_runs",
        "cdc_events",
        "contribution_revisions",
        "contribution_decisions",
        "cdc_projection_bindings",
        "cdc_projection_windows",
    }
    try:
        with engine.begin() as connection:
            quoted = str(
                literal(password).compile(dialect=engine.dialect, compile_kwargs={"literal_binds": True})
            )
            exists = connection.scalar(text("SELECT 1 FROM pg_roles WHERE rolname='ontweft_runtime'"))
            verb = "ALTER" if exists else "CREATE"
            connection.exec_driver_sql(
                f"{verb} ROLE ontweft_runtime LOGIN NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE PASSWORD {quoted}"
            )
            connection.execute(text("REVOKE CREATE ON SCHEMA public FROM PUBLIC"))
            connection.execute(text("GRANT USAGE ON SCHEMA public TO ontweft_runtime"))
            for table in register_models().sorted_tables:
                grant = (
                    "SELECT"
                    if table.name == "tenants"
                    else "SELECT, INSERT"
                    if table.name in immutable
                    else "SELECT, INSERT, UPDATE, DELETE"
                )
                name = engine.dialect.identifier_preparer.quote(table.name)
                connection.execute(text(f"GRANT {grant} ON TABLE {name} TO ontweft_runtime"))
            connection.execute(text("GRANT SELECT ON alembic_version TO ontweft_runtime"))
    except Exception:  # noqa: BLE001 - migration boundary must not print credential-bearing SQL
        raise SystemExit(
            "Runtime role provisioning failed; check database ownership and connectivity"
        ) from None
    finally:
        engine.dispose()
    print("Migrations and restricted runtime grants completed.")


if __name__ == "__main__":
    main()
