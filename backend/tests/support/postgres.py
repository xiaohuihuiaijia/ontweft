"""Real PostgreSQL tests; explicit admin/runtime URLs are required.

Each run creates and drops its own ontology_test_<uuid> database. No development
tenant or user data is modified. The application uses a NOBYPASSRLS login.
"""

import os
import subprocess
import sys
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from ontology.api import create_app
from ontology.cli import bootstrap
from ontology.core.config import Settings
from ontology.core.db import Base, Database
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url


@pytest.fixture(scope="module")
def pg_databases(tmp_path_factory):
    admin_value = os.environ.get("ONTOLOGY_TEST_ADMIN_URL")
    runtime_value = os.environ.get("ONTOLOGY_TEST_RUNTIME_URL")
    if not admin_value or not runtime_value:
        pytest.skip("Explicit PostgreSQL admin/runtime URLs required")
    name = "ontology_test_" + uuid4().hex
    admin_url = make_url(admin_value)
    runtime_url = make_url(runtime_value)
    provisioner = create_engine(admin_url, isolation_level="AUTOCOMMIT")
    with provisioner.connect() as connection:
        connection.execute(text(f'CREATE DATABASE "{name}"'))
    admin_url, runtime_url = admin_url.set(database=name), runtime_url.set(database=name)
    admin = Database(
        Settings(
            _env_file=None,
            database_url=admin_url.render_as_string(hide_password=False),
            data_dir=tmp_path_factory.mktemp("pg-admin"),
        )
    )
    try:
        result = subprocess.run(
            [sys.executable, "-c", ("from ontology.core.config import Settings; "
             "Settings.model_config['env_file'] = None; "
             "from alembic.config import main; main(['upgrade', 'head'])")],
            env={**os.environ, "ONTOLOGY_DATABASE_URL": admin_url.render_as_string(hide_password=False)},
            capture_output=True,
            check=False,
            text=True,
            timeout=60,
        )
        assert result.returncode == 0, result.stderr
        role = admin.engine.dialect.identifier_preparer.quote(runtime_url.username)
        with admin.engine.begin() as connection:
            connection.execute(text("REVOKE CREATE ON SCHEMA public FROM PUBLIC"))
            connection.execute(text(f"GRANT USAGE ON SCHEMA public TO {role}"))
            for table in Base.metadata.sorted_tables:
                grants = (
                    "SELECT"
                    if table.name == "tenants"
                    else "SELECT, INSERT"
                    if table.name
                    in {
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
                    else "SELECT, INSERT, UPDATE, DELETE"
                )
                connection.execute(text(f'GRANT {grants} ON TABLE "{table.name}" TO {role}'))
            connection.execute(text(f"GRANT SELECT ON alembic_version TO {role}"))
        yield admin, runtime_url.render_as_string(hide_password=False)
    finally:
        admin.engine.dispose()
        with provisioner.connect() as connection:
            connection.execute(text(f'DROP DATABASE "{name}" WITH (FORCE)'))
        provisioner.dispose()


@pytest.fixture
def pg_platform(pg_databases, tmp_path):
    admin, runtime_url = pg_databases
    password = "Postgres-Integration-Password"
    slug = "test-" + uuid4().hex
    a = bootstrap(admin, tenant_slug=slug, tenant_name="RLS A", email="owner@example.test", password=password)
    b = bootstrap(
        admin, tenant_slug=slug + "-b", tenant_name="RLS B", email="owner@example.test", password=password
    )
    app = create_app(
        Settings(
            _env_file=None,
            database_url=runtime_url,
            data_dir=tmp_path / "data",
            extensions=["knowledge", "integrations", "automation"],
        )
    )
    with TestClient(app) as client:
        login = client.post(
            "/api/auth/login", json={"tenant_slug": slug, "email": "owner@example.test", "password": password}
        )
        assert login.status_code == 200, login.text
        yield client, app, a, b
