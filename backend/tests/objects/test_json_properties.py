import json

import pytest

from tests.support.platform import publish
from tests.support.postgres import pg_databases as pg_databases  # noqa: PLC0414
from tests.support.postgres import pg_platform as pg_platform  # noqa: PLC0414


@pytest.mark.parametrize("fixture_name", ["platform", "pg_platform"])
@pytest.mark.parametrize("number", ["NaN", "Infinity", "-Infinity", "1e400"])
def test_json_property_rejects_nonfinite_numbers_without_committing(request, fixture_name, number):
    client, _, owner, _ = request.getfixturevalue(fixture_name)
    publish(
        client,
        owner["project_id"],
        {
            "types": [
                {
                    "key": "Item",
                    "label": "Item",
                    "properties": [{"key": "detail", "label": "Detail", "data_type": "json"}],
                }
            ]
        },
    )
    properties = '{"detail": {"items": [true, null, {"score": ' + number + "}]}}"
    payload = (
        '{"project_id": '
        + json.dumps(owner["project_id"])
        + ', "type_key": "Item", "properties": '
        + properties
        + "}"
    )
    response = client.post("/api/objects", content=payload, headers={"Content-Type": "application/json"})
    assert response.status_code == 422, response.text
    assert response.json()["error"]["code"] == "invalid_properties"
    assert client.get("/api/objects", params={"project_id": owner["project_id"]}).json()["total"] == 0
    valid = {"items": [True, None, {"score": 1.25}], "note": "nested JSON"}
    response = client.post(
        "/api/objects",
        json={"project_id": owner["project_id"], "type_key": "Item", "properties": {"detail": valid}},
    )
    assert response.status_code == 201, response.text
    saved = response.json()
    response = client.patch(
        f"/api/objects/{saved['id']}",
        content='{"expected_revision": 1, "properties": ' + properties + "}",
        headers={"Content-Type": "application/json"},
    )
    assert response.status_code == 422, response.text
    current = client.get(f"/api/objects/{saved['id']}").json()
    assert current["properties"] == {"detail": valid} and current["revision"] == 1
