import importlib.util
import json

import pytest
from fastapi.testclient import TestClient
from ontology.api import create_app
from ontology.cli import bootstrap
from ontology.core.config import Settings
from ontology.worker import process_one

from tests.support.paths import REPO_ROOT

spec = importlib.util.spec_from_file_location("experience_sample", REPO_ROOT / "scripts/experience_sample.py")
sample = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sample)


@pytest.fixture
def sample_workspace(tmp_path, monkeypatch):
    settings = Settings(
        _env_file=None,
        database_url=f"sqlite:///{tmp_path / 'sample.db'}",
        data_dir=tmp_path,
        model_approval="single",
        extensions=[],
        login_tenant_slug="sample",
    )
    app = create_app(settings)
    owner = bootstrap(
        app.state.db,
        tenant_slug="sample",
        tenant_name="合成工作空间",
        email="owner@example.test",
        username="owner",
        password="Synthetic-Sample-2026",
    )
    # Run the existing worker at the polling boundary, without a background service.
    monkeypatch.setattr(sample, "sleep", lambda seconds: process_one(app.state.db, settings))
    with TestClient(app, base_url="http://testserver/api/") as client:
        response = client.post("auth/login", json={"username": "owner", "password": "Synthetic-Sample-2026"})
        assert response.status_code == 200, response.text
        yield client, tmp_path, owner


def test_sample_uses_governed_api_and_preserves_user_changes_and_deletions(sample_workspace):
    client, data, owner = sample_workspace
    result = sample.ensure_sample(client, data)
    assert result["status"] == "complete"
    assert result["objects"] == 20 and result["relations"] == 24
    assert result["project_id"] != owner["project_id"]
    assert client.get("objects", params={"project_id": owner["project_id"]}).json()["total"] == 0
    project = result["project_id"]
    assert client.get("objects", params={"project_id": project}).json()["total"] == 20
    assert client.get("relations", params={"project_id": project}).json()["total"] == 24
    relation_id = result["relation_ids"]["suits:product-0:scenario-0"]
    relation = client.get(f"relations/{relation_id}").json()
    assert relation["properties"]["voltage"] == "24"
    assert relation["evidence"][0]["parse_id"]
    saved = client.patch(
        f"relations/{relation_id}",
        json={
            "expected_revision": relation["revision"],
            "expected_model_release_id": result["release_id"],
            "properties": {"condition": "用户修改后保留"},
        },
    )
    assert saved.status_code == 200, saved.text
    removed_id = result["relation_ids"]["contains:series-0:product-0"]
    removed = client.get(f"relations/{removed_id}").json()
    deleted = client.request(
        "DELETE",
        f"relations/{removed_id}",
        json={
            "expected_revision": removed["revision"],
            "expected_model_release_id": result["release_id"],
        },
    )
    assert deleted.status_code == 200, deleted.text
    assert sample.ensure_sample(client, data) == result
    assert client.get("relations", params={"project_id": project}).json()["total"] == 23
    assert client.get(f"relations/{relation_id}").json()["properties"]["condition"] == "用户修改后保留"
    assert sample.sample_is_complete(data)


def test_interrupted_api_response_resumes_without_duplicate_objects(sample_workspace, monkeypatch):
    client, data, _ = sample_workspace
    real_request = sample._request
    interrupted = False

    def lose_response(http, method, path, **kwargs):
        nonlocal interrupted
        result = real_request(http, method, path, **kwargs)
        if method == "POST" and path == "objects" and not interrupted:
            interrupted = True
            raise ConnectionError("Synthetic response lost after committed object")
        return result

    monkeypatch.setattr(sample, "_request", lose_response)
    with pytest.raises(ConnectionError):
        sample.ensure_sample(client, data)
    pending = sample.read_sample_state(data)
    assert pending["status"] == "pending"
    assert client.get("objects", params={"project_id": pending["project_id"]}).json()["total"] == 1
    monkeypatch.setattr(sample, "_request", real_request)
    result = sample.ensure_sample(client, data)
    assert client.get("objects", params={"project_id": result["project_id"]}).json()["total"] == 20
    assert client.get("relations", params={"project_id": result["project_id"]}).json()["total"] == 24
    assert client.get("documents", params={"project_id": result["project_id"]}).json()["total"] == 1
    assert client.get("changesets", params={"project_id": result["project_id"]}).json()["total"] == 1


def test_sample_does_not_adopt_an_unrelated_project(sample_workspace):
    client, data, _ = sample_workspace
    definition = json.loads((sample.SAMPLE_DIR / "sample.json").read_text(encoding="utf-8"))
    created = client.post("projects", json={"name": definition["project_name"]})
    assert created.status_code == 201
    with pytest.raises(ValueError, match="不会接管"):
        sample.ensure_sample(client, data)
    assert sample.read_sample_state(data) is None
