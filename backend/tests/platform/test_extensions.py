from fastapi.testclient import TestClient
from ontology.api import create_app
from ontology.cli import bootstrap
from ontology.core.config import Settings
from ontology.jobs.models import Job
from ontology.sources.models import Source
from ontology.worker import process_one
from sqlalchemy import select


def test_core_routes_and_parked_extension_jobs(tmp_path):
    settings = Settings(
        _env_file=None, database_url=f"sqlite:///{tmp_path / 'core.db'}", data_dir=tmp_path / "data"
    )
    app = create_app(settings)
    owner = bootstrap(
        app.state.db,
        tenant_slug="core",
        tenant_name="Core",
        email="owner@example.test",
        password="Core-Test-Password",
    )
    with TestClient(app) as client:
        assert (
            client.post(
                "/api/auth/login",
                json={"tenant_slug": "core", "email": "owner@example.test", "password": "Core-Test-Password"},
            ).status_code
            == 200
        )
        me = client.get("/api/auth/me").json()
        assert me["features"] == [] and me["model_approval"] == "single"
        for path in ("/documents", "/knowledge", "/functions", "/actions"):
            assert client.get("/api" + path, params={"project_id": owner["project_id"]}).status_code == 404
        assert (
            client.post(
                "/api/sources",
                json={"project_id": owner["project_id"], "name": "Optional", "kind": "mongo", "config": {}},
            ).status_code
            == 409
        )
        with app.state.db.transaction(owner["tenant_id"]) as session:
            legacy = Source(
                tenant_id=owner["tenant_id"],
                project_id=owner["project_id"],
                name="Retained optional source",
                kind="mongo",
                config={},
            )
            session.add(legacy)
            session.flush()
            legacy_id = legacy.id
            job = Job(
                tenant_id=owner["tenant_id"],
                project_id=owner["project_id"],
                owner_id=owner["user_id"],
                kind="document.parse",
                payload={},
            )
            session.add(job)
        assert any(
            row["id"] == legacy_id
            for row in client.get("/api/sources", params={"project_id": owner["project_id"]}).json()["items"]
        )
        assert client.post(f"/api/sources/{legacy_id}/test", json={}).status_code == 409
        assert (
            client.post(
                f"/api/sources/{legacy_id}/snapshots",
                json={"expected_source_revision": 1, "resource_id": "items"},
            ).status_code
            == 409
        )
        assert process_one(app.state.db, settings) is False
        with app.state.db.transaction(owner["tenant_id"]) as session:
            jobs = session.scalars(select(Job)).all()
            assert len(jobs) == 1 and jobs[0].status == "pending"
        assert client.get("/api/health").status_code == 200
    app.state.db.engine.dispose()


def test_separate_review_binds_current_author(platform, definition):
    client, app, owner, _ = platform
    app.state.settings.model_approval = "separate"
    change = client.post(
        "/api/changesets",
        json={"project_id": owner["project_id"], "title": "Current revision", "definition": definition},
    ).json()
    change = client.post(
        f"/api/changesets/{change['id']}/validate", json={"expected_revision": change["revision"]}
    ).json()
    response = client.post(
        f"/api/changesets/{change['id']}/approve", json={"expected_revision": change["revision"]}
    )
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "separate_reviewer_required"
    assert Settings(_env_file=None, environment="production").separate_model_approval
