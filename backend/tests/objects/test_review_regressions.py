import pytest
from ontology.core.models import uid
from ontology.documents.history import reference_status
from ontology.identity.models import Membership, Project
from ontology.objects.models import Entity
from ontology.worker import process_one
from sqlalchemy import event, insert

from tests.support.content import upload
from tests.support.document_reparse_pipeline import current, reparse
from tests.support.platform import publish
from tests.support.postgres import pg_databases as pg_databases  # noqa: PLC0414
from tests.support.postgres import pg_platform as pg_platform  # noqa: PLC0414


@pytest.mark.parametrize("fixture_name", ["platform", "pg_platform"])
def test_manual_object_and_relation_evidence_remains_bound_after_reparse(request, fixture_name, definition):
    client, app, owner, _ = request.getfixturevalue(fixture_name)
    publish(client, owner["project_id"], definition)
    created = upload(client, owner["project_id"])
    assert process_one(app.state.db, app.state.settings)
    doc = current(client, created["document"]["id"])
    reference = {
        "document_id": doc["id"],
        "revision": doc["revision"],
        "parse_revision": doc["parse_revision"],
        "ir_fingerprint": doc["ir_fingerprint"],
        "content_hash": doc["content_hash"],
    }
    objects = []
    for name in ("parent", "child"):
        response = client.post(
            "/api/objects",
            json={
                "project_id": owner["project_id"],
                "type_key": "Project",
                "properties": {"name": name},
                "evidence": [reference],
            },
        )
        assert response.status_code == 201, response.text
        objects.append(response.json())
    response = client.post(
        "/api/relations",
        json={
            "project_id": owner["project_id"],
            "source_id": objects[1]["id"],
            "target_id": objects[0]["id"],
            "type_key": "parent",
            "evidence": [reference],
        },
    )
    assert response.status_code == 201, response.text
    refs = [objects[0]["evidence"][0], response.json()["evidence"][0]]
    assert all(ref.get("parse_id") for ref in refs)
    assert refs[0] == refs[1]
    response = reparse(client, doc)
    assert response.status_code == 202, response.text
    assert process_one(app.state.db, app.state.settings)
    assert current(client, doc["id"])["parse_revision"] == doc["parse_revision"] + 1
    with app.state.db.transaction(owner["tenant_id"]) as session:
        project = session.get(Project, owner["project_id"])
        assert all(reference_status(session, project, ref)["status"] == "bound" for ref in refs)
    history = client.get(f"/api/objects/{objects[0]['id']}/history").json()["items"]
    assert history[0]["evidence"] == [refs[0]]


@pytest.mark.parametrize("fixture_name", ["platform", "pg_platform"])
@pytest.mark.parametrize("typed", [False, True])
def test_object_list_reuses_schema_without_per_object_queries(request, fixture_name, definition, typed):
    client, app, owner, _ = request.getfixturevalue(fixture_name)
    release = publish(client, owner["project_id"], definition)["release_id"]
    with app.state.db.transaction(owner["tenant_id"]) as session:
        session.execute(
            insert(Entity),
            [
                {
                    "id": uid(),
                    "tenant_id": owner["tenant_id"],
                    "project_id": owner["project_id"],
                    "type_key": "Project",
                    "release_id": release,
                    "properties": {"name": f"Public {i}", "budget": "193.25"},
                }
                for i in range(30)
            ],
        )
        from sqlalchemy import select

        session.scalar(
            select(Membership).where(
                Membership.project_id == owner["project_id"], Membership.user_id == owner["user_id"]
            )
        ).role = "member"
    statements = []

    def count(_connection, _cursor, statement, _parameters, _context, _many):
        if statement.lstrip().upper().startswith("SELECT"):
            statements.append(statement)

    event.listen(app.state.db.engine, "before_cursor_execute", count)
    try:
        counts = []
        for limit in (1, 20):
            statements.clear()
            params = {"project_id": owner["project_id"], "limit": limit}
            if typed:
                params["type_key"] = "Project"
            response = client.get("/api/objects", params=params)
            assert response.status_code == 200, response.text
            assert len(response.json()["items"]) == limit and response.json()["total"] == 30
            assert "budget" not in response.text and "193.25" not in response.text
            counts.append(len(statements))
        assert counts[1] <= counts[0] + 1, counts
    finally:
        event.remove(app.state.db.engine, "before_cursor_execute", count)
