import pytest
from ontology.core.models import uid
from ontology.objects.models import Entity, Relation
from sqlalchemy import insert

from tests.support.platform import publish
from tests.support.postgres import pg_databases as pg_databases  # noqa: PLC0414
from tests.support.postgres import pg_platform as pg_platform  # noqa: PLC0414


@pytest.mark.parametrize("fixture_name", ["platform", "pg_platform"])
def test_relation_pagination_reaches_every_authorized_active_relation(request, fixture_name, definition):
    client, app, owner, other = request.getfixturevalue(fixture_name)
    definition["types"][0]["relations"][0]["cardinality"] = "many"
    release = publish(client, owner["project_id"], definition)["release_id"]
    scope = {"tenant_id": owner["tenant_id"], "project_id": owner["project_id"]}
    center, targets = uid(), [uid() for _ in range(207)]
    visible = sorted(uid() for _ in range(205))
    with app.state.db.transaction(owner["tenant_id"]) as session:
        session.execute(
            insert(Entity),
            [
                {
                    **scope,
                    "id": id,
                    "type_key": "Project",
                    "release_id": release,
                    "properties": {"name": "Synthetic"},
                    "deleted": id == targets[-1],
                    "data_status": "incomplete" if id == targets[-2] else "active",
                }
                for id in [center, *targets]
            ],
        )
        session.execute(
            insert(Relation),
            [
                {**scope, "id": id, "type_key": "parent", "source_id": center, "target_id": target}
                for id, target in zip([*visible, uid(), uid()], targets)
            ],
        )
        session.add(Relation(**scope, source_id=targets[0], target_id=targets[1], type_key="parent"))
    params = {"project_id": owner["project_id"], "object_id": center}
    response = client.get("/api/relations", params=params)
    assert response.status_code == 200, response.text
    first = response.json()
    assert first["total"] == 205 and first["limit"] == 200 and first["offset"] == 0
    assert [row["id"] for row in first["items"]] == visible[:200]
    response = client.get("/api/relations", params={**params, "offset": 200})
    assert response.status_code == 200, response.text
    second = response.json()
    assert second["total"] == 205 and second["offset"] == 200
    assert [row["id"] for row in second["items"]] == visible[200:]
    exhausted = client.get("/api/relations", params={**params, "offset": 205}).json()
    assert exhausted["items"] == [] and exhausted["total"] == 205
    all_relations = client.get(
        "/api/relations", params={"project_id": owner["project_id"], "limit": 7, "offset": 200}
    ).json()
    assert all_relations["total"] == 206 and len(all_relations["items"]) == 6
    assert client.get("/api/relations", params={**params, "offset": -1}).status_code == 422
    assert client.get("/api/relations", params={"project_id": other["project_id"]}).status_code == 404
