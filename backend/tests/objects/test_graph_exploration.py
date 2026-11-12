import pytest

from tests.support.platform import publish
from tests.support.postgres import pg_databases as pg_databases  # noqa: PLC0414 - pytest fixture registration
from tests.support.postgres import pg_platform as pg_platform  # noqa: PLC0414 - pytest fixture registration


@pytest.mark.parametrize("fixture_name", ["platform", "pg_platform"])
def test_neighbor_cursor_scope_and_personal_view_reauthorization(request, fixture_name, definition):
    client, _app, owner, other = request.getfixturevalue(fixture_name)
    definition["types"][0]["relations"][0]["cardinality"] = "many"
    publish(client, owner["project_id"], definition)
    nodes = [
        client.post(
            "/api/objects",
            json={
                "project_id": owner["project_id"],
                "type_key": "Project",
                "properties": {"name": f"Node {i}", "budget": i},
            },
        ).json()
        for i in range(5)
    ]
    for target in nodes[1:]:
        assert (
            client.post(
                "/api/relations",
                json={
                    "project_id": owner["project_id"],
                    "type_key": "parent",
                    "source_id": nodes[0]["id"],
                    "target_id": target["id"],
                },
            ).status_code
            == 201
        )
    params = {"project_id": owner["project_id"], "object_id": nodes[0]["id"], "limit": 2}
    first = client.get("/api/graph/neighbors", params=params).json()
    second = client.get("/api/graph/neighbors", params={**params, "cursor": first["next_cursor"]}).json()
    assert len(first["edges"]) == len(second["edges"]) == 2 and second["next_cursor"] is None
    assert not {e["id"] for e in first["edges"]} & {e["id"] for e in second["edges"]}
    assert (
        client.get("/api/graph/neighbors", params={**params, "project_id": other["project_id"]}).status_code
        == 404
    )
    restored = client.post(
        "/api/graph/nodes",
        json={"project_id": owner["project_id"], "ids": [n["id"] for n in nodes] + ["not-visible"]},
    ).json()
    assert len(restored["nodes"]) == 5 and len(restored["edges"]) == 4
    assert (
        client.get(
            "/api/graph/neighbors", params={**params, "expected_model_release_id": "old-model"}
        ).status_code
        == 409
    )
