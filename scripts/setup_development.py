"""Provision the local development database after `alembic upgrade head`.

Reads the administrator URL from var/database-url.txt; never prints credentials.
The app uses a separate NOBYPASSRLS role. This script is local setup, not an API.
"""

import json
import secrets
from pathlib import Path

from ontology.cli import bootstrap
from ontology.core.config import Settings
from ontology.core.db import Base, Database
from ontology.identity.models import Tenant
from sqlalchemy import create_engine, select, text
from sqlalchemy.engine import make_url


def main():
    root = Path(__file__).resolve().parents[1]
    data = root / "var"
    admin_url = (data / "database-url.txt").read_text().strip()
    runtime_file = data / "runtime-database-url.txt"
    admin = create_engine(admin_url)
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
    with admin.begin() as connection:
        if not connection.scalar(text("SELECT 1 FROM pg_roles WHERE rolname = 'ontology_runtime'")):
            password = secrets.token_hex(32)
            connection.execute(
                text(
                    f"CREATE ROLE ontology_runtime LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOBYPASSRLS PASSWORD '{password}'"
                )
            )
            runtime_url = make_url(admin_url).set(username="ontology_runtime", password=password)
        elif not runtime_file.exists():
            raise RuntimeError("Runtime role already exists, but its local credentials file is missing")
        else:
            runtime_url = make_url(runtime_file.read_text().strip())
        connection.execute(text("REVOKE CREATE ON SCHEMA public FROM PUBLIC"))
        connection.execute(text("GRANT USAGE ON SCHEMA public TO ontology_runtime"))
        for table in Base.metadata.sorted_tables:
            grants = (
                "SELECT"
                if table.name == "tenants"
                else "SELECT, INSERT"
                if table.name in immutable
                else "SELECT, INSERT, UPDATE, DELETE"
            )
            connection.execute(text(f'REVOKE ALL ON TABLE "{table.name}" FROM ontology_runtime'))
            connection.execute(text(f'GRANT {grants} ON TABLE "{table.name}" TO ontology_runtime'))
        connection.execute(text("GRANT SELECT ON alembic_version TO ontology_runtime"))
    runtime_file.write_text(runtime_url.render_as_string(hide_password=False), encoding="utf-8")
    db = Database(Settings(database_url=admin_url, data_dir=data))
    login_file = data / "development-login.json"
    with db.transaction() as session:
        exists = session.scalar(select(Tenant.id).where(Tenant.slug == "development"))
    if not exists:
        password = secrets.token_urlsafe(24)
        ids = bootstrap(
            db,
            tenant_slug="development",
            tenant_name="本体开发企业",
            email="owner@example.test",
            username="admin",
            password=password,
        )
        login_file.write_text(
            json.dumps(
                {
                    "tenant_slug": "development",
                    "username": "admin",
                    "email": "owner@example.test",
                    "password": password,
                    **ids,
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
    elif not login_file.exists():
        raise RuntimeError("Development tenant exists; recover its credentials without overwriting it")
    db.engine.dispose()
    admin.dispose()
    print("Development database provisioned; credentials saved in ignored var/ files.")


if __name__ == "__main__":
    main()
