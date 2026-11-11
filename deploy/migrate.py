"""Apply reviewed migrations and grant the fixed least-privilege runtime role."""

import os

from alembic import command
from alembic.config import Config
from ontology.core.config import Settings
from ontology.core.registry import grant_runtime_access
from sqlalchemy import create_engine, literal, text


def main():
    password = os.environ.get("ONTOLOGY_RUNTIME_PASSWORD")
    if not password:
        raise SystemExit("Set ONTOLOGY_RUNTIME_PASSWORD before migration")
    command.upgrade(Config("alembic.ini"), "head")
    engine = create_engine(Settings().database_url)
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
            grant_runtime_access(connection, "ontweft_runtime")
    except Exception:  # noqa: BLE001 - migration boundary must not print credential-bearing SQL
        raise SystemExit(
            "Runtime role provisioning failed; check database ownership and connectivity"
        ) from None
    finally:
        engine.dispose()
    print("Migrations and restricted runtime grants completed.")


if __name__ == "__main__":
    main()
