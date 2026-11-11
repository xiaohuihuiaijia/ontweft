from contextlib import contextmanager

import pytest
from ontology.documents.history import capture_current_parse
from ontology.documents.parser import parse_document
from ontology.documents.storage_models import Document
from ontology.identity.models import Membership
from ontology.jobs.models import Job
from ontology.knowledge.models import KnowledgePage, KnowledgeRevision
from ontology.search.content.units import ir_fingerprint
from sqlalchemy import event, select

from tests.support.postgres import pg_databases as pg_databases  # noqa: PLC0414
from tests.support.postgres import pg_platform as pg_platform  # noqa: PLC0414


@contextmanager
def recorded_selects(app):
    statements = []

    def record(_connection, _cursor, statement, _parameters, _context, _many):
        if statement.lstrip().upper().startswith("SELECT"):
            statements.append(statement)

    event.listen(app.state.db.engine, "before_cursor_execute", record)
    try:
        yield statements
    finally:
        event.remove(app.state.db.engine, "before_cursor_execute", record)


def seed_documents(app, owner, count=12):
    ids = []
    with app.state.db.transaction(owner["tenant_id"]) as session:
        for index in range(count):
            doc = Document(
                tenant_id=owner["tenant_id"], project_id=owner["project_id"],
                filename=f"source-{index}.txt", content_hash="pending", storage_key=f"synthetic-{index}",
                uploader_id=owner["user_id"], size=24000, parse_revision=1, state="ready",
            )
            session.add(doc)
            session.flush()
            ir = parse_document(
                b"Synthetic evidence.\n" * 1000, doc.filename, source_id=doc.id, source_revision="1"
            ).model_dump(mode="json")
            doc.content_hash, doc.ir, doc.ir_fingerprint = ir["content_hash"], ir, ir_fingerprint(ir)
            snapshot = capture_current_parse(session, doc)
            job = Job(
                tenant_id=owner["tenant_id"], project_id=owner["project_id"], owner_id=owner["user_id"],
                kind="document.parse", payload={"document_id": doc.id, "engine": "basic"},
            )
            session.add(job)
            session.flush()
            ids.append((doc.id, snapshot.id, job.id))
    return ids


@pytest.mark.parametrize("fixture_name", ["platform", "pg_platform"])
def test_document_summaries_batch_metadata_without_loading_evidence(request, fixture_name):
    client, app, owner, other = request.getfixturevalue(fixture_name)
    ids = seed_documents(app, owner)
    counts = []
    with recorded_selects(app) as statements:
        for limit in (1, len(ids)):
            statements.clear()
            response = client.get("/api/documents", params={"project_id": owner["project_id"], "limit": limit})
            assert response.status_code == 200, response.text
            counts.append(len(statements))
            assert all("documents.ir," not in sql and "document_parse_versions.ir," not in sql for sql in statements)
        assert counts[1] <= counts[0] + 1, counts
    listed = {row["id"]: row for row in response.json()["items"]}
    for doc_id, parse_id, job_id in ids:
        row = listed[doc_id]
        assert "ir" not in row and "storage_key" not in row
        assert row["current_parse_id"] == parse_id
        assert row["active_parse_job"] == {"id": job_id, "status": "pending", "engine": "basic"}
    assert client.get("/api/documents", params={"project_id": other["project_id"]}).status_code == 404
    assert client.get(f"/api/documents/{ids[0][0]}").json()["ir"]["elements"]


@pytest.mark.parametrize("fixture_name", ["platform", "pg_platform"])
def test_parse_history_summary_omits_ir_reads_but_detail_retains_it(request, fixture_name):
    client, app, owner, _ = request.getfixturevalue(fixture_name)
    [(doc_id, parse_id, _)] = seed_documents(app, owner, count=1)
    with recorded_selects(app) as statements:
        response = client.get(f"/api/documents/{doc_id}/parses")
        assert response.status_code == 200, response.text
        assert all("documents.ir," not in sql and "document_parse_versions.ir," not in sql for sql in statements)
    row = response.json()["items"][0]
    assert row["is_current"] and "ir" not in row
    detail = client.get(f"/api/documents/{doc_id}/parses/{parse_id}").json()
    assert detail["ir"]["elements"] and detail["ir_json"]


def test_legacy_document_summary_still_derives_a_fingerprint(platform):
    client, app, owner, _ = platform
    [(doc_id, _, _)] = seed_documents(app, owner, count=1)
    with app.state.db.transaction(owner["tenant_id"]) as session:
        doc = session.get(Document, doc_id)
        expected = doc.ir_fingerprint
        doc.ir_fingerprint = None
    row = client.get("/api/documents", params={"project_id": owner["project_id"]}).json()["items"][0]
    assert row["ir_fingerprint"] == expected and "ir" not in row


@pytest.mark.parametrize("fixture_name", ["platform", "pg_platform"])
def test_knowledge_list_batches_published_revisions_without_loading_bodies(request, fixture_name):
    client, app, owner, other = request.getfixturevalue(fixture_name)
    with app.state.db.transaction(owner["tenant_id"]) as session:
        for index in range(12):
            page = KnowledgePage(
                tenant_id=owner["tenant_id"], project_id=owner["project_id"], author_id=owner["user_id"],
                title=f"Private draft {index}", body="Draft body" * 10000, references=[], revision=2,
                published_revision=1,
            )
            session.add(page)
            session.flush()
            session.add(KnowledgeRevision(
                tenant_id=owner["tenant_id"], project_id=owner["project_id"], author_id=owner["user_id"],
                page_id=page.id, revision=1, title=f"Published {index}", body="Published body" * 10000,
                references=[],
            ))
    # Owner sees draft metadata; readers see the published title/revision only.
    with recorded_selects(app) as statements:
        response = client.get("/api/knowledge", params={"project_id": owner["project_id"]})
        assert response.status_code == 200 and response.json()["items"][0]["title"].startswith("Private draft")
        assert all("knowledge_pages.body" not in sql for sql in statements)
    with app.state.db.transaction(owner["tenant_id"]) as session:
        session.scalar(select(Membership).where(
            Membership.project_id == owner["project_id"], Membership.user_id == owner["user_id"]
        )).role = "auditor"
    counts = []
    with recorded_selects(app) as statements:
        for limit in (1, 12):
            statements.clear()
            response = client.get("/api/knowledge", params={"project_id": owner["project_id"], "limit": limit})
            assert response.status_code == 200, response.text
            counts.append(len(statements))
            assert all("knowledge_pages.body" not in sql and "knowledge_revisions.body" not in sql for sql in statements)
        assert counts[1] <= counts[0] + 1, counts
    assert response.json()["total"] == 12
    assert all(row["title"].startswith("Published") and row["revision"] == 1 and "body" not in row for row in response.json()["items"])
    assert client.get("/api/knowledge", params={"project_id": other["project_id"]}).status_code == 404
    doc_id = response.json()["items"][0]["id"]
    assert client.get(f"/api/knowledge/{doc_id}").json()["body"].startswith("Published body")
