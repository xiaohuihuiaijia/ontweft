import pytest
from ontology.jobs.models import Job
from ontology.sources.models import SourceIdentity
from ontology.worker import process_one
from sqlalchemy import select

from tests.support.ingestion import capture
from tests.support.platform import publish
from tests.support.postgres import pg_databases as pg_databases  # noqa: PLC0414 - pytest fixture registration
from tests.support.postgres import pg_platform as pg_platform  # noqa: PLC0414 - pytest fixture registration


@pytest.mark.parametrize("failure", ["write", "flush", "commit_response"])
def test_failed_version_preview_removes_only_its_new_bundle(platform, monkeypatch, failure):
    from contextlib import contextmanager
    from pathlib import Path

    client, app, owner, _ = platform
    content = b"code,name\n001,Original\n"
    source = client.post(
        "/api/sources/upload",
        data={"project_id": owner["project_id"]},
        files={"file": ("data.csv", content)},
    ).json()
    source = client.post(
        f"/api/sources/{source['id']}/file-identity",
        json={"expected_revision": source["revision"], "business_keys": {"sheet_1": ["c1"]}},
    ).json()

    def preview():
        return client.post(
            f"/api/sources/{source['id']}/versions",
            data={"expected_revision": source["revision"]},
            files={"file": ("next.csv", b"code,name\n001,Changed\n")},
        )

    # Keep a previously committed preview as well as the active original.
    previous = preview()
    assert previous.status_code == 201, previous.text
    root = app.state.settings.data_dir / "uploaded_versions"
    before = {p.relative_to(root): p.read_bytes() for p in root.rglob("*") if p.is_file()}
    if failure == "write":
        original_open = Path.open

        def fail_write(path, *args, **kwargs):
            if path.name == "tables.json" and "uploaded_versions" in path.parts and args == ("xb",):
                raise OSError("synthetic write failure")
            return original_open(path, *args, **kwargs)

        monkeypatch.setattr(Path, "open", fail_write)
    elif failure == "flush":
        from sqlalchemy.orm import Session
        from ontology.sources.models import SourceFileVersion

        original_flush = Session.flush

        def fail_flush(session, *args, **kwargs):
            if any(isinstance(row, SourceFileVersion) for row in session.new):
                raise OSError("synthetic database flush failure")
            return original_flush(session, *args, **kwargs)

        monkeypatch.setattr(Session, "flush", fail_flush)
    else:
        transaction = app.state.db.transaction

        @contextmanager
        def fail_commit(*args, **kwargs):
            with transaction(*args, **kwargs) as session:
                yield session
            if kwargs.get("write"):
                raise OSError("synthetic lost commit response")

        monkeypatch.setattr(app.state.db, "transaction", fail_commit)
    with pytest.raises(OSError, match="synthetic"):
        preview()
    after = {p.relative_to(root): p.read_bytes() for p in root.rglob("*") if p.is_file()}
    assert all(after.get(path) == content for path, content in before.items())
    if failure == "commit_response":
        assert len(list(root.iterdir())) == 2
        versions = client.get(f"/api/sources/{source['id']}/versions").json()["items"]
        assert len(versions) == 3
        latest = max(versions, key=lambda version: version["number"])
        assert (
            client.get(f"/api/sources/{source['id']}/versions/{latest['id']}/original").content
            == b"code,name\n001,Changed\n"
        )
    else:
        assert after == before
        assert len(list(root.iterdir())) == 1
    assert client.get(f"/api/sources/{source['id']}/original").content == content
    if failure != "commit_response":
        assert len(client.get(f"/api/sources/{source['id']}/versions").json()["items"]) == 2


@pytest.mark.parametrize("fixture_name", ["platform", "pg_platform"])
def test_versions_update_by_business_key_preserve_history_and_withdraw_only_when_selected(
    request, fixture_name
):
    client, app, owner, _ = request.getfixturevalue(fixture_name)
    content = b"code,name\n001,Alpha\n002,Beta\n"
    source = client.post(
        "/api/sources/upload", data={"project_id": owner["project_id"]}, files={"file": ("data.csv", content)}
    ).json()
    configured = client.post(
        f"/api/sources/{source['id']}/file-identity",
        json={"expected_revision": source["revision"], "business_keys": {"sheet_1": ["c1"]}},
    )
    assert configured.status_code == 200, configured.text
    source = configured.json()
    proposed = client.post(
        f"/api/sources/{source['id']}/model-proposal",
        json={"expected_source_revision": source["revision"], "resource_ids": ["sheet_1"]},
    ).json()
    model = proposed["definition"]
    model["types"][0]["unique_keys"] = [["c1"]]
    publish(client, owner["project_id"], model)
    first = capture(client, app, source, source["resources"][0])
    assert first["snapshot"]["status"] == "complete", first
    with app.state.db.transaction(owner["tenant_id"]) as session:
        before = {
            identity.key_hash: identity.entity_id for identity in session.scalars(select(SourceIdentity))
        }

    def apply(content, mode="upsert"):
        nonlocal source
        preview = client.post(
            f"/api/sources/{source['id']}/versions",
            data={"expected_revision": source["revision"], "mode": mode},
            files={"file": ("next.csv", content)},
        )
        assert preview.status_code == 201, preview.text
        version = preview.json()
        response = client.post(
            f"/api/sources/{source['id']}/versions/{version['id']}/apply",
            json={"expected_revision": source["revision"]},
        )
        assert response.status_code == 200, response.text
        source = response.json()["source"]
        assert process_one(app.state.db, app.state.settings)
        with app.state.db.transaction(owner["tenant_id"]) as session:
            job = session.get(Job, response.json()["version"]["jobs"][0])
            assert job.status == "succeeded", job.error
        # A repeated confirmation returns the original job, never another update.
        assert (
            client.post(
                f"/api/sources/{source['id']}/versions/{version['id']}/apply", json={"expected_revision": 1}
            ).json()["version"]["jobs"]
            == response.json()["version"]["jobs"]
        )
        return version

    apply(b"code,name\n002,Beta updated\n001,Alpha\n003,Gamma\n")
    with app.state.db.transaction(owner["tenant_id"]) as session:
        after = {
            identity.key_hash: identity.entity_id for identity in session.scalars(select(SourceIdentity))
        }
        assert all(after[key] == value for key, value in before.items()) and len(after) == 3
    apply(b"code,name\n002,Beta updated\n")
    assert client.get("/api/objects", params={"project_id": owner["project_id"]}).json()["total"] == 3
    apply(b"code,name\n002,Beta updated\n", "replace")
    objects = client.get("/api/objects", params={"project_id": owner["project_id"]}).json()["items"]
    assert len(objects) == 1 and objects[0]["properties"]["c1"] == "002"
    versions = client.get(f"/api/sources/{source['id']}/versions").json()["items"]
    original = next(v for v in versions if v["number"] == 1)
    assert client.get(f"/api/sources/{source['id']}/versions/{original['id']}/original").content == content
    duplicate = client.post(
        f"/api/sources/{source['id']}/versions",
        data={"expected_revision": source["revision"]},
        files={"file": ("bad.csv", b"code,name\n002,Beta\n002,Other\n")},
    )
    assert duplicate.status_code == 422


@pytest.mark.parametrize("fixture_name", ["platform", "pg_platform"])
def test_exact_key_binding_reuses_object_and_retains_both_source_ledgers(request, fixture_name):
    client, app, owner, _ = request.getfixturevalue(fixture_name)
    from copy import deepcopy

    from ontology.objects.contributions.models import SourceContribution

    def upload(filename):
        response = client.post(
            "/api/sources/upload",
            data={"project_id": owner["project_id"]},
            files={"file": (filename, b"code,name\n001,Shared object\n")},
        )
        assert response.status_code == 201, response.text
        source = response.json()
        result = client.post(
            f"/api/sources/{source['id']}/file-identity",
            json={"expected_revision": source["revision"], "business_keys": {"sheet_1": ["c1"]}},
        )
        assert result.status_code == 200, result.text
        return result.json()

    first = upload("first.csv")
    proposal = client.post(
        f"/api/sources/{first['id']}/model-proposal",
        json={"expected_source_revision": first["revision"], "resource_ids": ["sheet_1"]},
    ).json()
    model = proposal["definition"]
    model["types"][0]["unique_keys"] = [["c1"]]
    publish(client, owner["project_id"], model)
    assert capture(client, app, first, first["resources"][0])["snapshot"]["status"] == "complete"
    original = client.get("/api/objects", params={"project_id": owner["project_id"]}).json()["items"][0]
    second = upload("second.csv")
    mapping = deepcopy(model["mappings"][0])
    mapping.update(
        key="second_source",
        source_id=second["id"],
        schema_fingerprint=second["resources"][0]["schema_fingerprint"],
        identity_fields=["c1"],
    )
    model["mappings"].append(mapping)
    publish(client, owner["project_id"], model)
    result = capture(client, app, second, second["resources"][0])
    assert result["snapshot"]["status"] == "complete", result
    rows = client.get("/api/objects", params={"project_id": owner["project_id"]}).json()["items"]
    assert len(rows) == 1 and rows[0]["id"] == original["id"]
    with app.state.db.transaction(owner["tenant_id"]) as session:
        identities = session.scalars(select(SourceIdentity)).all()
        assert len(identities) == 2 and {i.entity_id for i in identities} == {original["id"]}
        assert len(session.scalars(select(SourceContribution)).all()) == 2
