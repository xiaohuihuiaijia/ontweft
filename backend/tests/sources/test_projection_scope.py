import sqlite3
from copy import deepcopy

import pytest
from ontology.identity.models import User
from ontology.identity.security import access
from ontology.sources.ingestion import mapping_errors
from ontology.sources.models import Source

from tests.support.ingestion import capture, make_source
from tests.support.platform import publish
from tests.support.postgres import pg_databases as pg_databases  # noqa: PLC0414
from tests.support.postgres import pg_platform as pg_platform  # noqa: PLC0414


@pytest.mark.parametrize("fixture_name", ["platform", "pg_platform"])
def test_snapshot_before_first_model_keeps_raw_records(request, fixture_name):
    client, app, owner, _ = request.getfixturevalue(fixture_name)
    root = app.state.settings.data_dir / "imports"
    root.mkdir(parents=True, exist_ok=True)
    path = root / "raw.db"
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE items (id INTEGER PRIMARY KEY, name TEXT)")
        connection.execute("INSERT INTO items VALUES (1, 'sample')")
    response = client.post(
        "/api/sources",
        json={
            "project_id": owner["project_id"],
            "name": "Raw capture",
            "kind": "sqlite",
            "config": {"database": str(path)},
        },
    )
    assert response.status_code == 201, response.text
    source = response.json()
    resource = client.post(f"/api/sources/{source['id']}/discover").json()["resources"][0]
    captured = capture(client, app, source, resource)
    assert captured["snapshot"]["status"] == "complete", captured
    assert captured["snapshot"]["release_id"] is None
    assert captured["total"] == 1 and captured["items"][0]["values"]["name"] == "sample"
    assert captured["snapshot"]["projection"]["mappings"] == 0
    assert client.get("/api/objects", params={"project_id": owner["project_id"]}).json()["total"] == 0


@pytest.mark.parametrize("fixture_name", ["platform", "pg_platform"])
def test_snapshot_ignores_other_source_drift_but_publication_checks_it(request, fixture_name):
    client, app, owner, _ = request.getfixturevalue(fixture_name)
    source, resource, _, definition = make_source(client, app, owner["project_id"])
    with app.state.db.transaction(owner["tenant_id"]) as session:
        original = session.get(Source, source["id"])
        second = Source(
            tenant_id=owner["tenant_id"],
            project_id=owner["project_id"],
            name="Another source",
            kind=original.kind,
            resources=deepcopy(original.resources),
        )
        session.add(second)
        session.flush()
        second_id = second.id
    definition["mappings"].append(
        {**deepcopy(definition["mappings"][0]), "key": "other_inventory", "source_id": second_id}
    )
    publish(client, owner["project_id"], definition)
    with app.state.db.transaction(owner["tenant_id"]) as session:
        second = session.get(Source, second_id)
        second.resources = []
        ctx = access(session, session.get(User, owner["user_id"]), owner["project_id"])
        assert {error["mapping"] for error in mapping_errors(session, ctx, definition)} == {"other_inventory"}
    captured = capture(client, app, source, resource)
    assert captured["snapshot"]["status"] == "complete", captured
    assert captured["snapshot"]["projection"]["created"] == 2
    with app.state.db.transaction(owner["tenant_id"]) as session:
        session.get(Source, source["id"]).resources = []
        ctx = access(session, session.get(User, owner["user_id"]), owner["project_id"])
        errors = mapping_errors(
            session, ctx, definition, source_id=source["id"], resource_id=resource["resource_id"]
        )
        assert errors and {error["mapping"] for error in errors} == {"inventory_to_stock"}
