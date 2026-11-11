import pytest
from ontology.knowledge.models import KnowledgePage, KnowledgeRevision
from ontology.worker import process_one
from sqlalchemy import event

from tests.support.content import upload
from tests.support.postgres import pg_databases as pg_databases  # noqa: PLC0414
from tests.support.postgres import pg_platform as pg_platform  # noqa: PLC0414


@pytest.mark.parametrize("fixture_name", ["platform", "pg_platform"])
def test_knowledge_graph_batches_published_revisions_and_shared_documents(request, fixture_name):
    client, app, owner, other = request.getfixturevalue(fixture_name)
    uploaded = upload(client, owner["project_id"])
    assert process_one(app.state.db, app.state.settings)
    doc = client.get(f"/api/documents/{uploaded['document']['id']}").json()
    reference = {
        "document_id": doc["id"],
        "revision": doc["revision"],
        "parse_id": doc["current_parse_id"],
        "parse_revision": doc["parse_revision"],
        "ir_fingerprint": doc["ir_fingerprint"],
        "content_hash": doc["content_hash"],
    }
    with app.state.db.transaction(owner["tenant_id"]) as session:
        for index in range(20):
            page = KnowledgePage(
                tenant_id=owner["tenant_id"],
                project_id=owner["project_id"],
                title="Unpublished secret",
                body="Draft only",
                references=[reference] * 5,
                revision=2,
                published_revision=1,
                author_id=owner["user_id"],
            )
            session.add(page)
            session.flush()
            for revision in (1, 2):
                session.add(
                    KnowledgeRevision(
                        tenant_id=owner["tenant_id"],
                        project_id=owner["project_id"],
                        page_id=page.id,
                        revision=revision,
                        title=f"Published {index}" if revision == 1 else page.title,
                        body="Body",
                        references=[reference] * 5,
                        author_id=owner["user_id"],
                    )
                )
    statements = []

    def count(_connection, _cursor, statement, _parameters, _context, _many):
        if statement.lstrip().upper().startswith("SELECT"):
            statements.append(statement)

    event.listen(app.state.db.engine, "before_cursor_execute", count)
    try:
        counts = []
        for limit in (1, 40):
            statements.clear()
            response = client.get(
                "/api/graph", params={"project_id": owner["project_id"], "kind": "knowledge", "limit": limit}
            )
            assert response.status_code == 200, response.text
            assert "Unpublished secret" not in response.text
            counts.append(len(statements))
        graph = response.json()
        assert len(graph["nodes"]) == 21 and len(graph["edges"]) == 100 and not graph["truncated"]
        assert all(edge["target"] == doc["id"] for edge in graph["edges"])
        assert counts[1] <= counts[0] + 1, counts
        assert (
            client.get(
                "/api/graph", params={"project_id": other["project_id"], "kind": "knowledge"}
            ).status_code
            == 404
        )
    finally:
        event.remove(app.state.db.engine, "before_cursor_execute", count)
