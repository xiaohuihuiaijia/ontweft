"""Model semantics and object revision are independent optimistic baselines."""

from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from threading import Event

import ontology.modeling.api as ontology_api
import ontology.objects.api as objects_api
import pytest
from ontology.audit.models import Audit
from ontology.identity.models import Project
from ontology.objects.models import Entity, EntityRevision, Relation
from sqlalchemy import func, select

from tests.support.platform import publish
from tests.support.postgres import pg_databases as pg_databases  # noqa: PLC0414 - pytest fixture registration
from tests.support.postgres import pg_platform as pg_platform  # noqa: PLC0414 - pytest fixture registration


def setup_objects(client, owner, definition):
    release = publish(client, owner["project_id"], definition)["release_id"]
    objects = []
    for name in ("one", "two", "three"):
        response = client.post(
            "/api/objects",
            json={
                "project_id": owner["project_id"],
                "type_key": "Project",
                "properties": {"name": name},
                "expected_model_release_id": release,
            },
        )
        assert response.status_code == 201, response.text
        objects.append(response.json())
    response = client.post(
        "/api/relations",
        json={
            "project_id": owner["project_id"],
            "type_key": "parent",
            "source_id": objects[0]["id"],
            "target_id": objects[1]["id"],
            "expected_model_release_id": release,
        },
    )
    assert response.status_code == 201, response.text
    return release, objects, response.json()


def changed_definition(definition):
    definition = deepcopy(definition)
    definition["types"][0]["label"] = "Current project"
    definition["types"][0]["relations"][0]["label"] = "Current parent semantics"
    return definition


def write_operation(client, owner, objects, relation, operation, baseline):
    if operation == "create":
        return client.post(
            "/api/objects",
            json={
                "project_id": owner["project_id"],
                "type_key": "Project",
                "properties": {"name": "new"},
                **baseline,
            },
        )
    if operation == "update":
        return client.patch(
            f"/api/objects/{objects[0]['id']}",
            json={
                "expected_revision": objects[0]["revision"],
                "properties": {"name": "edited"},
                **baseline,
            },
        )
    if operation == "relation_create":
        return client.post(
            "/api/relations",
            json={
                "project_id": owner["project_id"],
                "type_key": "parent",
                "source_id": objects[2]["id"],
                "target_id": objects[1]["id"],
                **baseline,
            },
        )
    return client.request(
        "DELETE",
        f"/api/relations/{relation['id']}",
        json={
            "expected_revision": relation["revision"],
            **baseline,
        },
    )


def counts(db, owner):
    with db.transaction(owner["tenant_id"]) as session:
        return tuple(
            session.scalar(
                select(func.count()).select_from(model).where(model.project_id == owner["project_id"])
            )
            for model in (Entity, EntityRevision, Relation, Audit)
        )


def test_read_model_baseline_is_distinct_from_object_last_write_release(platform, definition):
    client, _, owner, _ = platform
    previous, objects, _ = setup_objects(client, owner, definition)
    published = publish(client, owner["project_id"], changed_definition(definition))["release_id"]
    result = client.get(f"/api/objects/{objects[0]['id']}")
    assert result.status_code == 200
    value = result.json()
    assert value["release_id"] == previous
    assert value["model_release_id"] == published
    assert value["model_version"] == 2
    assert value["revision"] == objects[0]["revision"]
    listing = client.get("/api/objects", params={"project_id": owner["project_id"]}).json()
    assert all(item["model_release_id"] == published for item in listing["items"])


@pytest.mark.parametrize("operation", ["create", "update", "relation_create", "relation_delete"])
def test_stale_model_rejects_writes_without_objects_history_or_audit_changes(platform, definition, operation):
    client, app, owner, _ = platform
    previous, objects, relation = setup_objects(client, owner, definition)
    current = publish(client, owner["project_id"], changed_definition(definition))["release_id"]
    before = counts(app.state.db, owner)
    response = write_operation(
        client, owner, objects, relation, operation, {"expected_model_release_id": previous}
    )
    assert response.status_code == 409, response.text
    assert response.json()["error"]["code"] == "revision_conflict"
    assert counts(app.state.db, owner) == before
    unchanged = client.get(f"/api/objects/{objects[0]['id']}").json()
    assert unchanged["revision"] == objects[0]["revision"]
    assert unchanged["properties"] == objects[0]["properties"]
    assert unchanged["model_release_id"] == current
    accepted = write_operation(
        client, owner, objects, relation, operation, {"expected_model_release_id": current}
    )
    assert accepted.status_code in {200, 201}, accepted.text


@pytest.mark.parametrize("operation", ["create", "update", "relation_create", "relation_delete"])
def test_explicit_null_is_checked_but_omission_preserves_current_model_mode(platform, definition, operation):
    client, app, owner, _ = platform
    _, objects, relation = setup_objects(client, owner, definition)
    before = counts(app.state.db, owner)
    response = write_operation(
        client, owner, objects, relation, operation, {"expected_model_release_id": None}
    )
    assert response.status_code == 409, response.text
    assert counts(app.state.db, owner) == before
    # Compatibility only: omission deliberately makes no fixed-model promise.
    accepted = write_operation(client, owner, objects, relation, operation, {})
    assert accepted.status_code in {200, 201}, accepted.text


def test_explicit_null_matches_empty_model_without_bypassing_type_validation(platform):
    client, _, owner, _ = platform
    response = client.post(
        "/api/objects",
        json={
            "project_id": owner["project_id"],
            "type_key": "undefined",
            "properties": {},
            "expected_model_release_id": None,
        },
    )
    # Null == no active model. The subsequent ordinary type check still fails.
    assert response.status_code == 422, response.text
    assert response.json()["error"]["code"] == "unknown_type"


def test_postgres_queued_write_rechecks_actual_model_publication_with_preloaded_identity_map(
    request, definition, monkeypatch
):
    client, app, owner, _ = request.getfixturevalue("pg_platform")
    previous, objects, _ = setup_objects(client, owner, definition)
    response = client.post(
        "/api/changesets",
        json={
            "project_id": owner["project_id"],
            "title": "Concurrent release",
            "definition": changed_definition(definition),
            "expected_baseline_release_id": previous,
        },
    )
    assert response.status_code == 201, response.text
    change = response.json()
    for operation in ("validate", "approve"):
        response = client.post(
            f"/api/changesets/{change['id']}/{operation}", json={"expected_revision": change["revision"]}
        )
        assert response.status_code == 200, response.text
        change = response.json()

    publication_holds_lock, release_publication, writer_preloaded = Event(), Event(), Event()
    original_audit, original_access = ontology_api.audit, objects_api.access

    def pause_publication(session, user, project_id, event_name, *args, **kwargs):
        original_audit(session, user, project_id, event_name, *args, **kwargs)
        if event_name == "ontology.publish":
            publication_holds_lock.set()
            assert release_publication.wait(15), "Test did not release the actual publication transaction"

    def access_with_preloaded_project(session, user, project_id, permission="objects:read"):
        if permission == "objects:write":
            # Retain a real ORM identity before the blocked FOR UPDATE read.
            # READ COMMITTED sees the old committed release at this moment.
            old = session.get(Project, project_id)
            assert old.active_release_id == previous
            writer_preloaded.set()
            result = original_access(session, user, project_id, permission)
            assert result.project is old
            return result
        return original_access(session, user, project_id, permission)

    monkeypatch.setattr(ontology_api, "audit", pause_publication)
    monkeypatch.setattr(objects_api, "access", access_with_preloaded_project)
    before = counts(app.state.db, owner)
    with ThreadPoolExecutor(max_workers=2) as pool:
        publisher = pool.submit(
            client.post,
            f"/api/changesets/{change['id']}/publish",
            json={"expected_revision": change["revision"]},
        )
        try:
            assert publication_holds_lock.wait(10)
            writer = pool.submit(
                client.patch,
                f"/api/objects/{objects[0]['id']}",
                json={
                    "expected_revision": objects[0]["revision"],
                    "properties": {"name": "stale meaning"},
                    "expected_model_release_id": previous,
                },
            )
            assert writer_preloaded.wait(10)
            assert not writer.done(), "The writer must wait behind the real publication lock"
        finally:
            release_publication.set()
        published = publisher.result(timeout=15)
        assert published.status_code == 200, published.text
        rejected = writer.result(timeout=15)
        assert rejected.status_code == 409, rejected.text
    after = counts(app.state.db, owner)
    assert after[:3] == before[:3]
    assert after[3] == before[3] + 1  # Only the successful model publication audit.
    value = client.get(f"/api/objects/{objects[0]['id']}").json()
    assert value["revision"] == objects[0]["revision"]
    assert value["properties"] == objects[0]["properties"]
    assert value["release_id"] == previous
    assert value["model_release_id"] == published.json()["release_id"]
