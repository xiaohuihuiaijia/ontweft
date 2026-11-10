"""Real bytes -> parser subprocess -> durable index -> evidence and download.

These tests use isolated SQLite platform fixtures. PostgreSQL authorization and
search SQL have separate integration coverage; no parser or index is mocked.
"""

import hashlib

import pytest
from ontology.documents.history import parse_payload
from ontology.documents.storage_models import Document
from ontology.jobs.models import Job
from ontology.search.content.index import process_one_content_index


def drain(app, owner):
    for _ in range(50):
        if not process_one_content_index(app.state.db, owner["tenant_id"]):
            break
    else:
        pytest.fail("Real content index did not become idle")


def search(client, owner, text):
    response = client.post(
        "/api/search/content",
        json={
            "project_id": owner["project_id"],
            "q": text,
            "kind": "document",
            "evidence_mode": "original_evidence",
        },
    )
    assert response.status_code == 200, response.text
    return response.json()


def resolve(client, owner, hit):
    return client.post(
        "/api/search/content/evidence",
        json={
            "project_id": owner["project_id"],
            "chunk_id": hit["id"],
            "expected_generation": hit["generation"],
            "expected_fingerprint": hit["source_fingerprint"],
            "expected_revision": hit["source_revision"],
            "expected_parse_revision": hit["parse_revision"],
            "evidence_mode": "original_evidence",
        },
    )


def assert_bound_download(client, owner, hit, doc, original):
    response = resolve(client, owner, hit)
    assert response.status_code == 200, response.text
    result = response.json()
    unit, anchor = result["unit"], hit["anchor"]
    assert unit["chunk_text"] == hit["excerpt"]
    assert unit["text"][anchor["unit_text_start"] : anchor["unit_text_end"]] == hit["excerpt"]
    assert unit["evidence"]["source_id"] == doc["id"]
    assert unit["evidence"]["source_revision"] == str(doc["revision"])
    assert unit["evidence"]["content_hash"] == hashlib.sha256(original).hexdigest()
    download = client.get(result["download_url"])
    assert download.status_code == 200, download.text if download.status_code != 200 else ""
    assert download.content == original
    assert download.headers["x-content-type-options"] == "nosniff"
    return result


def queue_reparse(app, owner, doc_id, engine):
    # This older index regression queues a real baseline-bound Job directly.
    # The public reparse endpoint is covered by test_document_reparse_pipeline.
    with app.state.db.transaction(owner["tenant_id"]) as session:
        doc = session.get(Document, doc_id)
        assert doc.tenant_id == owner["tenant_id"] and doc.project_id == owner["project_id"]
        job = Job(
            tenant_id=owner["tenant_id"],
            project_id=owner["project_id"],
            owner_id=owner["user_id"],
            kind="document.parse",
            payload=parse_payload(doc, engine, doc.ir["format"]),
        )
        session.add(job)
        session.flush()
        return job.id
