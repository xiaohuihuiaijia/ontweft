"""Public reparse requests with real parser subprocesses and retained evidence."""

import copy
import io

import pytest
from ontology import worker
from ontology.documents.history_models import DocumentParseVersion
from ontology.documents.storage_models import Document
from ontology.jobs.models import Job
from sqlalchemy import func, select

from tests.support.content import upload
from tests.support.content_search_pipeline import drain, resolve, search
from tests.support.document_reparse_pipeline import (
    assert_job,
    assert_retained,
    current,
    historical,
    history,
    parsed_markdown,
    reparse,
)
from tests.support.documents import _native_pdf_sample


def test_public_word_reparse_preserves_published_historical_reference(platform):
    docx = pytest.importorskip("docx")
    pytest.importorskip("docling")
    client, app, owner, _ = platform
    source = docx.Document()
    source.add_heading("设备维修", 1)
    source.add_paragraph("阀门每季度检查一次。")
    output = io.BytesIO()
    source.save(output)
    original = output.getvalue()
    created = upload(client, owner["project_id"], original, "维修手册.docx")
    assert worker.process_one(app.state.db, app.state.settings)
    old_doc = current(client, created["document"]["id"])
    assert old_doc["state"] == "ready", old_doc.get("error")
    old_summary = history(client, old_doc["id"])["items"][0]
    assert "ir" not in old_summary
    old = historical(client, old_doc["id"], old_summary["id"])
    assert old_doc["current_parse_id"] == old["id"]
    assert old["is_current"] and old["origin"] == "worker"
    assert old["ir"] == old_doc["ir"]
    element = next(e for e in old["ir"]["elements"] if "阀门" in e["text"])
    reference = {
        "document_id": old_doc["id"],
        "revision": old_doc["revision"],
        "parse_id": old["id"],
        "parse_revision": old["parse_revision"],
        "ir_fingerprint": old["ir_fingerprint"],
        "content_hash": old["content_hash"],
        "element_path": element["evidence"]["element_path"],
    }
    response = client.post(
        "/api/knowledge",
        json={
            "project_id": owner["project_id"],
            "title": "已审核维保",
            "body": "阀门检查周期已确认。",
            "references": [reference],
        },
    )
    assert response.status_code == 201, response.text
    page = response.json()
    response = client.post(
        f"/api/knowledge/{page['id']}/publish", json={"expected_revision": page["revision"]}
    )
    assert response.status_code == 200, response.text
    drain(app, owner)
    hit = search(client, owner, "阀门")["items"][0]
    requested = reparse(client, old_doc)
    assert requested.status_code == 202, requested.text
    pending = current(client, old_doc["id"])
    assert pending["current_parse_id"] == old["id"]
    assert pending["active_parse_job"]["id"] == requested.json()["job"]["id"]
    assert reparse(client, old_doc).status_code == 409
    assert search(client, owner, "阀门")["total"] == 1
    assert resolve(client, owner, hit).status_code == 200
    assert worker.process_one(app.state.db, app.state.settings)
    new_doc = current(client, old_doc["id"])
    assert new_doc["parse_revision"] == 2 and new_doc["revision"] == 1
    assert new_doc["ir_fingerprint"] != old_doc["ir_fingerprint"]
    assert new_doc["content_hash"] == old_doc["content_hash"]
    assert new_doc["current_parse_id"] != old["id"] and new_doc["active_parse_job"] is None
    assert_job(app, owner, requested.json()["job"]["id"], "succeeded")
    versions = history(client, old_doc["id"])
    assert versions["total"] == 2
    retained = historical(client, old_doc["id"], old["id"])
    assert retained["ir"] == old["ir"] and not retained["is_current"]
    assert retained["configuration_fingerprint"] == old["configuration_fingerprint"]
    assert client.get(retained["download_url"]).content == original
    page_now = client.get(f"/api/knowledge/{page['id']}").json()
    assert page_now["references"] == [reference]
    # Historical parsing evidence remains publishable; it is not rebound to the
    # new parser's element paths even when source bytes have not changed.
    response = client.post(
        f"/api/knowledge/{page['id']}/publish", json={"expected_revision": page["revision"]}
    )
    assert response.status_code == 200, response.text
    assert resolve(client, owner, hit).status_code == 409
    drain(app, owner)
    assert search(client, owner, "阀门")["items"][0]["parse_revision"] == 2


@pytest.mark.parametrize(
    "altered",
    [b"Different valid Markdown.", b"\xff\xff\xff"],
    ids=["source-hash-mismatch", "parser-reported-error"],
)
def test_public_reparse_failure_keeps_successful_ir_and_index(platform, altered):
    client, app, owner, _ = platform
    old, original = parsed_markdown(platform)
    hit = search(client, owner, "阀门")["items"][0]
    requested = reparse(client, old)
    assert requested.status_code == 202, requested.text
    with app.state.db.transaction(owner["tenant_id"]) as session:
        doc = session.get(Document, old["id"])
        path = app.state.settings.data_dir / "assets" / doc.storage_key
    # Exercise both the exception path (valid text with another source hash)
    # and the parser's explicit unsuccessful-result path (invalid UTF-8).
    path.write_bytes(altered)
    try:
        assert worker.process_one(app.state.db, app.state.settings)
        assert_job(app, owner, requested.json()["job"]["id"], "failed")
        assert_retained(client, app, owner, old, hit)
    finally:
        path.write_bytes(original)
    retry = client.post(f"/api/jobs/{requested.json()['job']['id']}/retry")
    assert retry.status_code == 200, retry.text
    assert worker.process_one(app.state.db, app.state.settings)
    assert current(client, old["id"])["parse_revision"] == 2
    assert history(client, old["id"])["total"] == 2


def test_cancelled_reparse_retains_old_and_cannot_retry_over_a_new_request(platform):
    client, app, owner, _ = platform
    old, _ = parsed_markdown(platform)
    hit = search(client, owner, "阀门")["items"][0]
    first = reparse(client, old)
    assert first.status_code == 202, first.text
    first_id = first.json()["job"]["id"]
    assert client.post(f"/api/jobs/{first_id}/cancel").status_code == 200
    assert_retained(client, app, owner, old, hit)
    second = reparse(client, old)
    assert second.status_code == 202, second.text
    assert client.post(f"/api/jobs/{first_id}/retry").status_code == 409
    assert worker.process_one(app.state.db, app.state.settings)
    assert current(client, old["id"])["parse_revision"] == 2
    assert client.post(f"/api/jobs/{first_id}/retry").status_code == 409
    assert_job(app, owner, first_id, "cancelled")


def test_cancel_after_real_parser_finishes_discards_late_result_and_can_retry(platform, monkeypatch):
    client, app, owner, _ = platform
    old, _ = parsed_markdown(platform)
    hit = search(client, owner, "阀门")["items"][0]
    requested = reparse(client, old)
    assert requested.status_code == 202, requested.text
    job_id = requested.json()["job"]["id"]
    real_run = worker.subprocess.run
    cancelled = False

    def cancel_before_commit(*args, **kwargs):
        nonlocal cancelled
        result = real_run(*args, **kwargs)
        command = args[0] if args else kwargs.get("args", [])
        if not cancelled and "ontology.parse_task" in command:
            cancelled = True
            response = client.post(f"/api/jobs/{job_id}/cancel")
            assert response.status_code == 200, response.text
        return result

    monkeypatch.setattr(worker.subprocess, "run", cancel_before_commit)
    assert worker.process_one(app.state.db, app.state.settings)
    assert cancelled
    assert_job(app, owner, job_id, "cancelled")
    assert_retained(client, app, owner, old, hit)
    assert client.post(f"/api/jobs/{job_id}/retry").status_code == 200
    assert worker.process_one(app.state.db, app.state.settings)
    assert_job(app, owner, job_id, "succeeded")
    assert current(client, old["id"])["parse_revision"] == 2
    assert history(client, old["id"])["total"] == 2


def test_different_job_late_real_parser_result_cannot_replace_newer_parse(platform, monkeypatch):
    client, app, owner, _ = platform
    old, _ = parsed_markdown(platform)
    first = reparse(client, old)
    assert first.status_code == 202, first.text
    first_id = first.json()["job"]["id"]
    real_run = worker.subprocess.run
    newer_id = None

    def complete_newer_after_real_parse(*args, **kwargs):
        nonlocal newer_id
        result = real_run(*args, **kwargs)
        command = args[0] if args else kwargs.get("args", [])
        if newer_id is None and "ontology.parse_task" in command:
            # Deliberately bypass the public API's mutual exclusion to exercise
            # worker defense against a second independent Job, not just another
            # fencing token for the same Job. Both parsers actually execute.
            with app.state.db.transaction(owner["tenant_id"]) as session:
                original_job = session.get(Job, first_id)
                newer = Job(
                    tenant_id=owner["tenant_id"],
                    project_id=owner["project_id"],
                    owner_id=owner["user_id"],
                    kind="document.parse",
                    payload=copy.deepcopy(original_job.payload),
                )
                session.add(newer)
                session.flush()
                newer_id = newer.id
            assert worker.process_one(app.state.db, app.state.settings, "newer-independent-parser")
        return result

    monkeypatch.setattr(worker.subprocess, "run", complete_newer_after_real_parse)
    assert worker.process_one(app.state.db, app.state.settings, "older-independent-parser")
    assert newer_id is not None
    assert_job(app, owner, first_id, "failed")
    assert_job(app, owner, newer_id, "succeeded")
    doc = current(client, old["id"])
    assert doc["parse_revision"] == 2 and doc["state"] == "ready" and doc["error"] is None
    versions = history(client, old["id"])
    assert versions["total"] == 2
    assert next(v for v in versions["items"] if v["parse_revision"] == 2)["job_id"] == newer_id


def test_configuration_changed_after_queue_does_not_reinterpret_reparse(platform, monkeypatch):
    pytest.importorskip("docling")
    pytest.importorskip("pypdfium2")
    monkeypatch.setenv("ONTOLOGY_DOCLING_PDF_MODE", "native")
    client, app, owner, _ = platform
    created = upload(client, owner["project_id"], _native_pdf_sample(), "native.pdf")
    assert worker.process_one(app.state.db, app.state.settings)
    old = current(client, created["document"]["id"])
    assert old["state"] == "ready", old.get("error")
    requested = reparse(client, old, "docling")
    assert requested.status_code == 202, requested.text
    monkeypatch.setenv("ONTOLOGY_DOCLING_PDF_MODE", "layout")
    assert worker.process_one(app.state.db, app.state.settings)
    assert_job(app, owner, requested.json()["job"]["id"], "failed")
    saved = current(client, old["id"])
    assert saved["state"] == "ready" and saved["ir"] == old["ir"]
    assert saved["parse_revision"] == 1
    with app.state.db.transaction(owner["tenant_id"]) as session:
        assert (
            session.scalar(
                select(func.count())
                .select_from(DocumentParseVersion)
                .where(
                    DocumentParseVersion.document_id == old["id"],
                )
            )
            == 1
        )
