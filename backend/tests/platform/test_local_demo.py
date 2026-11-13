import importlib.util
import os
import subprocess
import sys

import pytest
from fastapi.testclient import TestClient
from ontology.connectors import ConnectorError
from ontology.connectors.policy import NetworkPolicy

from tests.support.paths import REPO_ROOT as ROOT

spec = importlib.util.spec_from_file_location("ontweft_local_demo", ROOT / "scripts/start_local_demo.py")
demo = importlib.util.module_from_spec(spec)
spec.loader.exec_module(demo)


def test_local_demo_restarts_without_reset_and_ignores_external_config(tmp_path, monkeypatch):
    # A fake build is sufficient here; the real bundle is exercised by the browser script.
    assets = tmp_path / "source/frontend/dist"
    assets.mkdir(parents=True)
    (assets / "index.html").write_text("<h1>local test</h1>", encoding="utf-8")
    monkeypatch.setattr(demo, "ROOT", tmp_path / "source")
    monkeypatch.setenv("ONTOLOGY_DATABASE_URL", "postgresql+pg8000://invalid.invalid/never-connect")
    monkeypatch.setenv("ONTOLOGY_LOGIN_TENANT_SLUG", "unrelated")
    monkeypatch.setenv("ONTOLOGY_EXTENSIONS", '["knowledge","integrations","automation"]')
    monkeypatch.setenv("ONTOLOGY_MODEL_APPROVAL", "separate")
    data = tmp_path / "isolated-demo"
    password = "Local-Test-Only-2026"
    with TestClient(demo.create_demo_app(data, password=password)) as client:
        assert client.get("/api/health").json()["storage"] == "sqlite"
        assert client.get("/").text == "<h1>local test</h1>"
        assert (
            client.post("/api/auth/login", json={"username": "owner", "password": password}).status_code
            == 200
        )
        session = client.get("/api/auth/me").json()
        assert session["features"] == []
        assert session["model_approval"] == "single"
        project = session["projects"][0]["id"]
        definition = client.get("/api/ontology/templates", params={"project_id": project}).json()["items"][0][
            "definition"
        ]
        saved = client.post(
            "/api/changesets",
            json={
                "project_id": project,
                "title": "保留我的草稿",
                "definition": definition,
                "expected_baseline_release_id": None,
            },
        )
        assert saved.status_code == 201, saved.text
        draft = saved.json()["id"]
    with TestClient(demo.create_demo_app(data)) as restarted:
        assert (
            restarted.post("/api/auth/login", json={"username": "owner", "password": password}).status_code
            == 200
        )
        drafts = restarted.get("/api/changesets", params={"project_id": project}).json()["items"]
        assert [item["id"] for item in drafts] == [draft]


def test_local_demo_refuses_an_existing_unowned_directory(tmp_path):
    data = tmp_path / "other"
    data.mkdir()
    existing = data / "demo.db"
    existing.write_bytes(b"must not be opened or replaced")
    with pytest.raises(ValueError, match="已有其他文件"):
        demo.create_demo_app(data, password="Local-Test-Only-2026")
    assert existing.read_bytes() == b"must not be opened or replaced"


def test_local_demo_accepts_documents_above_the_old_25_mib_limit(tmp_path, monkeypatch):
    assets = tmp_path / "source/frontend/dist"
    assets.mkdir(parents=True)
    (assets / "index.html").write_text("<h1>local test</h1>", encoding="utf-8")
    monkeypatch.setattr(demo, "ROOT", tmp_path / "source")
    monkeypatch.setenv("ONTOLOGY_MAX_UPLOAD_BYTES", "1024")
    path = tmp_path / "large.txt"
    with path.open("wb") as stream:
        for _ in range(26):
            stream.write(b"x" * 1024 * 1024)
    password = "Local-Test-Only-2026"
    with TestClient(demo.create_demo_app(tmp_path / "demo", password=password)) as client:
        client.post("/api/auth/login", json={"username": "owner", "password": password}).raise_for_status()
        project = client.get("/api/auth/me").json()["projects"][0]["id"]
        with path.open("rb") as stream:
            response = client.post(
                "/api/documents/upload",
                data={"project_id": project},
                files={"file": (path.name, stream, "text/plain")},
            )
        assert response.status_code == 201, response.text


def test_source_network_requires_an_explicit_host_and_separate_private_opt_in(tmp_path, monkeypatch):
    monkeypatch.setenv("ONTOLOGY_ALLOWED_SOURCE_HOSTS", '["ambient.example"]')
    monkeypatch.setenv("ONTOLOGY_ALLOW_PRIVATE_SOURCE_ADDRESSES", "true")
    defaults = demo.demo_settings(tmp_path, 8020)
    assert defaults.allowed_source_hosts == [] and not defaults.allow_private_source_addresses
    settings = demo.demo_settings(tmp_path, 8020, source_hosts=["127.0.0.1", "DB.Example.", "db.example"])
    assert settings.allowed_source_hosts == ["127.0.0.1", "db.example"]
    with pytest.raises(ConnectorError, match="Private"):
        NetworkPolicy(frozenset(settings.allowed_source_hosts)).check_host("127.0.0.1")
    enabled = demo.demo_settings(tmp_path, 8020, source_hosts=["127.0.0.1"], allow_private_sources=True)
    policy = NetworkPolicy(frozenset(enabled.allowed_source_hosts), enabled.allow_private_source_addresses)
    policy.check_host("127.0.0.1")
    with pytest.raises(ConnectorError, match="explicitly"):
        policy.check_host("127.0.0.2")
    with pytest.raises(ValueError, match="指定主机"):
        demo.demo_settings(tmp_path, 8020, allow_private_sources=True)


@pytest.mark.parametrize(
    "host", ["*", "https://db.example", "db.example:5432", "user@db.example", "", "db.example/path"]
)
def test_source_host_arguments_do_not_accept_urls_ports_or_wildcards(tmp_path, host):
    with pytest.raises(ValueError, match="来源主机"):
        demo.demo_settings(tmp_path, 8020, source_hosts=[host])


def test_core_boot_does_not_import_unused_feishu_sdk(tmp_path):
    # Use a fresh process: other integration tests intentionally load the SDK.
    # Fail on an attempted import rather than relying on a machine-specific timer.
    code = """
import importlib.abc
import sys
from pathlib import Path

class ForbidUnusedSdk(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname == 'lark_oapi' or fullname.startswith('lark_oapi.'):
            raise AssertionError('Core startup must not load the unused Feishu SDK')

sys.meta_path.insert(0, ForbidUnusedSdk())
from fastapi.testclient import TestClient
from ontology.api import create_app
from ontology.core.config import Settings

data = Path(sys.argv[1])
app = create_app(Settings(_env_file=None, data_dir=data,
    database_url='sqlite:///' + (data / 'core.db').as_posix(), environment='development'))
with TestClient(app) as client:
    assert client.get('/api/health').json()['storage'] == 'sqlite'
assert 'lark_oapi' not in sys.modules
"""
    env = {key: value for key, value in os.environ.items() if not key.startswith("ONTOLOGY_")}
    env["PYTHONPATH"] = str(ROOT / "backend")
    result = subprocess.run(
        [sys.executable, "-c", code, str(tmp_path)],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, result.stderr


def test_local_demo_generation_upgrade_backs_up_and_preserves_history(tmp_path):
    import json
    import sqlite3

    from tests.support.cdc_evidence_migrations import historical_stream, isolated_database, seed_old

    with isolated_database(tmp_path, "sqlite") as (database, migrate, _):
        seeded = seed_old(database)
        migrate("ef311029a400")
        with database.transaction(seeded["owner"]["tenant_id"]) as session:
            old = historical_stream(
                session, seeded["owner"], seeded["source_id"], seeded["owner"]["project_id"]
            )
        data = tmp_path / "demo"
        data.mkdir()
        (data / demo.MARKER).write_text(json.dumps(demo.PROFILE), encoding="utf-8")
        with (
            sqlite3.connect(database.engine.url.database) as source,
            sqlite3.connect(data / "demo.db") as target,
        ):
            source.backup(target)
        backup = demo.upgrade_demo_schema(data)
        assert backup.is_file() and not demo.needs_schema_upgrade(data)
        with sqlite3.connect(backup) as connection:
            assert "previous_stream_id" not in {
                row[1] for row in connection.execute("PRAGMA table_info(cdc_streams)")
            }
            assert connection.execute("SELECT id FROM cdc_streams").fetchone()[0] == old.id
        with sqlite3.connect(data / "demo.db") as connection:
            assert connection.execute("SELECT id,previous_stream_id FROM cdc_streams").fetchone() == (
                old.id,
                None,
            )
            assert not connection.execute("PRAGMA foreign_key_check").fetchall()
        assert demo.upgrade_demo_schema(data) is None


def test_local_demo_adds_model_notes_to_existing_proposal_rows(tmp_path):
    import json
    import sqlite3

    from ontology.cli import bootstrap
    from ontology.core.db import Database
    from ontology.intelligence.models import ModelConnection
    from ontology.intelligence.proposal_models import AIProposal

    data = tmp_path / "existing-demo"
    db = Database(demo.demo_settings(data, 8020))
    try:
        owner = bootstrap(
            db,
            tenant_slug="local-demo",
            tenant_name="Synthetic upgrade",
            email="owner@example.test",
            username="owner",
            password="Local-Test-Only-2026",
        )
        with db.transaction() as session:
            connection = ModelConnection(
                tenant_id=owner["tenant_id"],
                name="Unused synthetic connection",
                base_url="https://not-used.example/v1",
                enabled=False,
            )
            session.add(connection)
            session.flush()
            proposal = AIProposal(
                tenant_id=owner["tenant_id"],
                project_id=owner["project_id"],
                owner_id=owner["user_id"],
                connection_id=connection.id,
                connection_revision=1,
                inputs={"requirement": "保留旧提案内容"},
                status="ready",
            )
            session.add(proposal)
            session.flush()
            proposal_id = proposal.id
        with db.engine.begin() as connection:
            connection.exec_driver_sql("ALTER TABLE ai_proposals DROP COLUMN model_notes")
        db.engine.dispose()
        (data / demo.MARKER).write_text(json.dumps(demo.PROFILE), encoding="utf-8")
        assert demo.needs_schema_upgrade(data)
        with pytest.raises(ValueError, match="migrate"):
            demo.create_demo_app(data)

        backup = demo.upgrade_demo_schema(data)
        assert backup.is_file() and not demo.needs_schema_upgrade(data)
        with sqlite3.connect(backup) as connection:
            assert "model_notes" not in {
                row[1] for row in connection.execute("PRAGMA table_info(ai_proposals)")
            }
            assert connection.execute("SELECT id FROM ai_proposals").fetchone() == (proposal_id,)
        with db.transaction() as session:
            restored = session.get(AIProposal, proposal_id)
            assert restored.inputs == {"requirement": "保留旧提案内容"}
            assert restored.model_notes == [] and restored.status == "ready"
        assert demo.upgrade_demo_schema(data) is None
        assert len(list(data.glob("demo-before-upgrade-*.db"))) == 1
    finally:
        db.engine.dispose()
