import json
import sqlite3

import pytest
from ontology.objects.models import Entity

from tests.support.platform import publish
from tests.support.postgres import pg_databases as pg_databases  # noqa: PLC0414 - pytest fixture registration
from tests.support.postgres import pg_platform as pg_platform  # noqa: PLC0414 - pytest fixture registration


@pytest.mark.parametrize("fixture_name", ["platform", "pg_platform"])
def test_large_integer_edit_query_and_history_round_trip_without_browser_rounding(request, fixture_name):
    client, app, tenant, _ = request.getfixturevalue(fixture_name)
    project = tenant["project_id"]
    publish(
        client,
        project,
        {
            "types": [
                {
                    "key": "Counter",
                    "label": "精确计数",
                    "properties": [
                        {"key": "count", "label": "计数", "data_type": "integer"},
                    ],
                }
            ]
        },
    )
    exact = "9223372036854775809"
    response = client.post(
        "/api/objects", json={"project_id": project, "type_key": "Counter", "properties": {"count": exact}}
    )
    assert response.status_code == 201, response.text
    obj = response.json()
    assert obj["properties"]["count"] == exact
    with app.state.db.transaction(tenant["tenant_id"]) as session:
        assert session.get(Entity, obj["id"]).properties["count"] == int(exact)
    assert client.get(f"/api/objects/{obj['id']}").json()["properties"]["count"] == exact
    query = {
        "project_id": project,
        "type_key": "Counter",
        "filters": [{"field": "count", "op": "eq", "value": exact}],
    }
    result = client.post("/api/query", json=query)
    assert result.status_code == 200, result.text
    assert result.json()["items"][0]["properties"]["count"] == exact
    response = client.patch(
        f"/api/objects/{obj['id']}",
        json={"expected_revision": 1, "properties": {"count": "-9223372036854775810"}},
    )
    assert response.status_code == 200, response.text
    assert response.json()["properties"]["count"] == "-9223372036854775810"
    history = client.get(f"/api/objects/{obj['id']}/history").json()["items"]
    assert [row["properties"]["count"] for row in history] == ["-9223372036854775810", exact]
    graph = client.get("/api/graph", params={"project_id": project}).json()
    assert graph["nodes"][0]["properties"]["count"] == "-9223372036854775810"
    for invalid in ("1.1", "1e3", " 100", 1.5, True, "1" * 1001):
        response = client.patch(
            f"/api/objects/{obj['id']}", json={"expected_revision": 2, "properties": {"count": invalid}}
        )
        assert response.status_code == 422, (invalid, response.text)
    safe = client.patch(
        f"/api/objects/{obj['id']}", json={"expected_revision": 2, "properties": {"count": "42"}}
    )
    assert safe.status_code == 200 and safe.json()["properties"]["count"] == 42


def test_source_sample_and_raw_evidence_keep_exact_integer_text(platform):
    from tests.support.ingestion import capture, make_source

    client, app, owner, _ = platform
    source, resource, path, _ = make_source(client, app, owner["project_id"])
    exact = 9223372036854775807
    with sqlite3.connect(path) as connection:
        connection.execute("UPDATE inventory SET id=? WHERE id=1", (exact,))
    sample = client.get(f"/api/sources/{source['id']}/sample", params={"resource": resource["resource_id"]})
    assert sample.status_code == 200, sample.text
    assert f'"id":{exact}' in sample.json()["sample_json"]
    assert json.loads(sample.json()["sample_json"]) == {
        key: value for key, value in sample.json().items() if key != "sample_json"
    }
    captured = capture(client, app, source, resource)
    row = next(item for item in captured["items"] if item["source_key"]["id"] == exact)
    assert row["source_key_json"] == f'{{"id":{exact}}}'
    assert f'"id":{exact}' in row["values_json"]
    response = client.get(f"/api/source-records/{row['id']}")
    assert response.status_code == 200, response.text
    assert f'"id":{exact}' in response.json()["record_json"]
    assert json.loads(response.json()["record_json"])["content_hash"] == row["content_hash"]
