"""Data-to-model-to-graph using a real PostgreSQL reader, never a mocked source."""

from datetime import UTC, datetime

import pytest
from ontology.connectors import ConnectorError, NetworkPolicy, SQLConfig, SQLConnector
from sqlalchemy import text

from tests.support.ingestion import capture
from tests.support.intake_database import intake_database as intake_database  # noqa: PLC0414
from tests.support.platform import publish
from tests.support.postgres import pg_databases as pg_databases  # noqa: PLC0414
from tests.support.postgres import pg_platform as pg_platform  # noqa: PLC0414


@pytest.mark.parametrize("fixture_name", ["platform", "pg_platform"])
def test_real_postgres_source_to_editable_model_objects_relations_and_updates(
    request, intake_database, fixture_name
):
    client, app, owner, _ = request.getfixturevalue(fixture_name)
    fixture = intake_database
    app.state.settings.allowed_source_hosts = [fixture["config"]["host"]]
    app.state.settings.allow_private_source_addresses = True
    response = client.post(
        "/api/sources",
        json={
            "project_id": owner["project_id"],
            "name": "合成 PostgreSQL 来源",
            "kind": "postgres",
            "config": fixture["config"],
            "credentials": fixture["credentials"],
        },
    )
    assert response.status_code == 201, response.text
    source = response.json()
    assert "secret" not in source and "password" not in source["config"]
    assert client.post(f"/api/sources/{source['id']}/test").json()["ok"]
    discovered = client.post(f"/api/sources/{source['id']}/discover")
    assert discovered.status_code == 200, discovered.text
    source = discovered.json()["source"]
    resources = {item["name"]: item for item in source["resources"]}
    assert set(resources) == {"items", "groups"}
    assert resources["items"]["foreign_keys"][0]["target_fields"] == ["code"]
    draft = client.post(
        f"/api/sources/{source['id']}/model-proposal",
        json={
            "expected_source_revision": source["revision"],
            "resource_ids": [resources[key]["resource_id"] for key in ("groups", "items")],
        },
    )
    assert draft.status_code == 200, draft.text
    model = draft.json()["definition"]
    types = {item["key"]: item for item in model["types"]}
    assert {prop["key"]: prop["data_type"] for prop in types["items"]["properties"]} == {
        "id": "integer",
        "name": "string",
        "group_code": "string",
        "amount": "number",
        "occurred_on": "date",
        "enabled": "boolean",
        "payload": "json",
        "observed_at": "datetime",
        "local_at": "string",
        "tags": "json",
        "reference": "string",
    }
    types["items"]["label"] = "我的业务条目"
    types["items"]["properties"].append({"key": "note", "label": "人工备注", "data_type": "string"})
    types["items"]["relations"] = [
        {"key": "belongs_to", "label": "所属分组", "target_type": "groups", "cardinality": "one"}
    ]
    publish(client, owner["project_id"], model)
    for resource in resources.values():
        result = capture(client, app, source, resource)
        assert result["snapshot"]["status"] == "complete", result
        assert result["snapshot"]["projection"]["created"] == 2
    objects = client.get("/api/objects", params={"project_id": owner["project_id"]}).json()["items"]
    item = next(row for row in objects if row["type_key"] == "items" and row["properties"]["id"] == 1)
    assert item["properties"]["amount"] == "9007199254740993.20"
    assert item["properties"]["occurred_on"] == "2026-01-02"
    assert item["properties"]["enabled"] is True
    assert item["properties"]["payload"] == {"origin": "synthetic"}
    assert datetime.fromisoformat(item["properties"]["observed_at"]) == datetime(
        2026, 1, 2, 0, 30, tzinfo=UTC
    )
    assert item["properties"]["local_at"] == "2026-01-02T08:30:00"
    assert item["properties"]["tags"] == ["甲", "乙"]
    assert item["properties"]["reference"] == "00000000-0000-4000-8000-000000000001"
    release = client.get("/api/ontology/definition", params={"project_id": owner["project_id"]}).json()[
        "release_id"
    ]
    rule = {
        "project_id": owner["project_id"],
        "expected_model_release_id": release,
        "source_type": "items",
        "relation_key": "belongs_to",
        "source_field": "group_code",
        "target_field": "code",
    }
    preview = client.post("/api/relations/match-preview", json=rule)
    assert preview.status_code == 200, preview.text
    assert preview.json()["counts"]["create"] == 2
    applied = client.post(
        "/api/relations/match-apply", json={"rule": rule, "expected_plan_hash": preview.json()["plan_hash"]}
    )
    assert applied.status_code == 200 and applied.json()["created"] == 2
    graph = client.get("/api/graph", params={"project_id": owner["project_id"]}).json()
    assert len(graph["nodes"]) == 4 and len(graph["edges"]) == 2
    edited = client.patch(
        f"/api/objects/{item['id']}",
        json={"expected_revision": item["revision"], "properties": {"note": "保留人工补充"}},
    )
    assert edited.status_code == 200, edited.text
    with fixture["admin"].begin() as connection:
        connection.execute(
            text(f"UPDATE {fixture['schema']}.items SET name='来源更新后的名称', amount=2.30 WHERE id=1")
        )
    updated = capture(client, app, source, resources["items"])
    assert updated["snapshot"]["status"] == "complete", updated
    assert updated["snapshot"]["projection"]["created"] == 0
    current = client.get(f"/api/objects/{item['id']}").json()
    assert current["properties"]["name"] == "来源更新后的名称"
    assert current["properties"]["amount"] == "2.30"
    assert current["properties"]["note"] == "保留人工补充"
    assert client.get("/api/objects", params={"project_id": owner["project_id"]}).json()["total"] == 4
    assert client.get("/api/relations", params={"project_id": owner["project_id"]}).json()["total"] == 2

    # The source account itself cannot modify even this isolated synthetic database.
    connector = SQLConnector(
        SQLConfig(driver="postgres", **fixture["config"], **fixture["credentials"]),
        network_policy=NetworkPolicy(
            frozenset(app.state.settings.allowed_source_hosts), allow_private_addresses=True
        ),
    )
    try:
        with pytest.raises(ConnectorError):
            connector._rows(f"DELETE FROM {fixture['schema']}.items")
        assert len(connector._rows(f"SELECT id FROM {fixture['schema']}.items")) == 2
    finally:
        connector.close()
