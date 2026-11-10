"""Public reparse requests with real parser subprocesses and retained evidence."""

from ontology import worker
from ontology.jobs.models import Job

from tests.support.content import upload
from tests.support.content_search_pipeline import drain, resolve, search


def current(client, document_id):
    response = client.get(f"/api/documents/{document_id}")
    assert response.status_code == 200, response.text
    return response.json()


def history(client, document_id):
    response = client.get(f"/api/documents/{document_id}/parses")
    assert response.status_code == 200, response.text
    return response.json()


def historical(client, document_id, parse_id):
    response = client.get(f"/api/documents/{document_id}/parses/{parse_id}")
    assert response.status_code == 200, response.text
    return response.json()


def reparse(client, doc, engine="basic"):
    return client.post(
        f"/api/documents/{doc['id']}/reparse",
        json={
            "expected_revision": doc["revision"],
            "expected_parse_revision": doc["parse_revision"],
            "expected_ir_fingerprint": doc["ir_fingerprint"],
            "engine": engine,
        },
    )


def parsed_markdown(platform):
    client, app, owner, _ = platform
    original = "# 设备手册\n\n阀门每季度检查一次。".encode()
    created = upload(client, owner["project_id"], original, "保养手册.md")
    assert worker.process_one(app.state.db, app.state.settings)
    doc = current(client, created["document"]["id"])
    assert doc["state"] == "ready", doc.get("error")
    drain(app, owner)
    return doc, original


def assert_job(app, owner, job_id, expected):
    with app.state.db.transaction(owner["tenant_id"]) as session:
        assert session.get(Job, job_id).status == expected


def assert_retained(client, app, owner, doc, hit):
    saved = current(client, doc["id"])
    for key in ("ir", "ir_fingerprint", "parse_revision", "revision", "content_hash", "state"):
        assert saved[key] == doc[key], key
    assert history(client, doc["id"])["total"] == 1
    drain(app, owner)
    assert search(client, owner, "阀门")["total"] == 1
    assert resolve(client, owner, hit).status_code == 200
