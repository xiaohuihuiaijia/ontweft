import pytest
from fastapi.testclient import TestClient
from ontology.identity.models import Tenant
from ontology.objects.models import Entity
from sqlalchemy import event, insert

from tests.support.platform import publish
from tests.support.postgres import pg_databases as pg_databases  # noqa: PLC0414 - pytest fixture registration
from tests.support.postgres import pg_platform as pg_platform  # noqa: PLC0414 - pytest fixture registration


@pytest.mark.parametrize("fixture_name", ["platform", "pg_platform"])
def test_graph_collection_has_constant_query_count_without_leaking_fields(request, fixture_name, definition):
    client, app, owner, other = request.getfixturevalue(fixture_name)
    project = owner["project_id"]
    definition["types"][0]["properties"].append({"key": "count", "label": "计数", "data_type": "integer"})
    publish(client, project, definition)
    baseline = client.get("/api/ontology/definition", params={"project_id": project}).json()["release_id"]
    with app.state.db.transaction(owner["tenant_id"]) as session:
        tenant_slug = session.get(Tenant, owner["tenant_id"]).slug
        session.execute(insert(Entity), [{"id": f"scale-{i:04d}", "tenant_id": owner["tenant_id"],
            "project_id": project, "type_key": "Project", "release_id": baseline,
            "properties": {"name": f"公开名称 {i}", "budget": "999.12", "count": 9223372036854775809}}
            for i in range(120)])
    person = client.post("/api/users", json={"email": "graph-reader@example.test", "display_name": "只读成员",
        "password": "Synthetic-Graph-Reader-2026"}).json()
    added = client.post("/api/members", json={"project_id": project, "user_id": person["id"], "role": "member"})
    assert added.status_code == 200
    statements = []

    def count(_connection, _cursor, statement, _parameters, _context, _many):
        if statement.lstrip().upper().startswith("SELECT"):
            statements.append(statement)

    event.listen(app.state.db.engine, "before_cursor_execute", count)
    try:
        with TestClient(app) as member:
            login = member.post("/api/auth/login", json={"tenant_slug": tenant_slug,
                "email": "graph-reader@example.test", "password": "Synthetic-Graph-Reader-2026"})
            assert login.status_code == 200
            counts = []
            for limit in (10, 100):
                statements.clear()
                response = member.get("/api/graph", params={"project_id": project, "limit": limit})
                assert response.status_code == 200, response.text
                counts.append(len(statements))
                graph = response.json()
                assert len(graph["nodes"]) == limit and graph["truncated"] is True
                assert graph["release_id"] == baseline
                assert "budget" not in response.text and "999.12" not in response.text
                assert all(node["properties"]["count"] == "9223372036854775809" for node in graph["nodes"])
            # Covers accidental per-object schema reads without a wall-clock assertion.
            assert counts[1] <= counts[0] + 2, counts
            assert member.get("/api/graph", params={"project_id": other["project_id"]}).status_code == 404
    finally:
        event.remove(app.state.db.engine, "before_cursor_execute", count)
