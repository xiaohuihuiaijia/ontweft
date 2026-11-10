import importlib.util
import os
import subprocess
import sys

import pytest
from fastapi.testclient import TestClient

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
