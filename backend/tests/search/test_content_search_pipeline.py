"""Real bytes -> parser subprocess -> durable index -> evidence and download.

These tests use isolated SQLite platform fixtures. PostgreSQL authorization and
search SQL have separate integration coverage; no parser or index is mocked.
"""

import hashlib
import io

import pytest
from ontology.documents.storage_models import Document
from ontology.jobs.models import Job
from ontology.search.content.units import ir_fingerprint
from ontology.worker import process_one

from tests.support.content import upload
from tests.support.content_search_pipeline import assert_bound_download, drain, queue_reparse, resolve, search
from tests.support.documents import _native_pdf_sample


def test_real_word_chinese_table_evidence_download_and_reparse(platform):
    docx = pytest.importorskip("docx")
    pytest.importorskip("docling")
    client, app, owner, _ = platform
    word = docx.Document()
    word.add_heading("设备维修", 1)
    word.add_paragraph("阀门每季度检查一次。")
    table = word.add_table(rows=2, cols=2)
    table.cell(0, 0).merge(table.cell(1, 0))
    table.cell(0, 0).text = "Calibration"
    table.cell(0, 1).text = "检查"
    table.cell(1, 1).text = "复核"
    data = io.BytesIO()
    word.save(data)
    original = data.getvalue()
    uploaded = upload(client, owner["project_id"], original, "维修手册.docx")
    assert uploaded["job"]["payload"]["engine"] == "docling"
    assert process_one(app.state.db, app.state.settings)
    doc = client.get(f"/api/documents/{uploaded['document']['id']}").json()
    assert doc["state"] == "ready", doc.get("error")
    assert doc["ir"]["parser"] == "docling/docx"
    assert doc["parse_revision"] == 1 and doc["revision"] == 1
    assert doc["ir_fingerprint"] == ir_fingerprint(doc["ir"])
    drain(app, owner)
    chinese = search(client, owner, "阀门")
    assert chinese["total"] == 1
    hit = chinese["items"][0]
    assert hit["source_fingerprint"] == doc["ir_fingerprint"] and hit["parse_revision"] == 1
    result = assert_bound_download(client, owner, hit, doc, original)
    assert result["unit"]["text"] == "阀门每季度检查一次。"
    assert result["unit"]["evidence"]["page_number"] is None
    assert result["unit"]["evidence"]["bbox"] is None
    english = search(client, owner, "Calibration")
    assert english["total"] == 1  # Merged cell is not repeated via aggregate table.text.
    cell_hit = english["items"][0]
    assert cell_hit["anchor"]["unit_kind"] == "table_cell"
    cell = assert_bound_download(client, owner, cell_hit, doc, original)["unit"]
    assert cell["metadata"]["table_cell"]["row_span"] == 2
    assert cell["metadata"]["provenance_scope"] == "parent_table"
    assert cell["evidence"]["page_number"] is None

    # Reparse the same immutable bytes with another installed real parser.
    # The source revision/hash stay fixed; parser revision and full IR hash change.
    queue_reparse(app, owner, doc["id"], "basic")
    assert search(client, owner, "阀门")["total"] == 1
    assert resolve(client, owner, hit).status_code == 200
    assert process_one(app.state.db, app.state.settings)
    reparsed = client.get(f"/api/documents/{doc['id']}").json()
    assert reparsed["state"] == "ready", reparsed.get("error")
    assert reparsed["parse_revision"] == 2 and reparsed["revision"] == 1
    assert reparsed["content_hash"] == doc["content_hash"]
    assert reparsed["ir_fingerprint"] == ir_fingerprint(reparsed["ir"])
    assert reparsed["ir_fingerprint"] != doc["ir_fingerprint"]
    assert search(client, owner, "阀门")["total"] == 0  # Old index cannot stand in for a new parse.
    drain(app, owner)
    new = search(client, owner, "阀门")["items"][0]
    assert new["parse_revision"] == 2
    assert resolve(client, owner, hit).status_code == 409
    assert_bound_download(client, owner, new, reparsed, original)


@pytest.mark.parametrize("with_blank_page", [False, True], ids=["complete", "partial"])
def test_real_native_pdf_preserves_boxes_and_partial_coverage(platform, monkeypatch, with_blank_page):
    pytest.importorskip("docling")
    pytest.importorskip("pypdfium2")
    monkeypatch.setenv("ONTOLOGY_DOCLING_PDF_MODE", "native")
    client, app, owner, _ = platform
    original = _native_pdf_sample()
    if with_blank_page:
        from pypdf import PdfReader, PdfWriter

        writer = PdfWriter()
        writer.add_blank_page(width=612, height=792)
        writer.add_page(PdfReader(io.BytesIO(original)).pages[0])
        output = io.BytesIO()
        writer.write(output)
        original = output.getvalue()
    uploaded = upload(client, owner["project_id"], original, "native-evidence.pdf")
    assert uploaded["job"]["payload"]["engine"] == "docling"
    assert process_one(app.state.db, app.state.settings)
    doc = client.get(f"/api/documents/{uploaded['document']['id']}").json()
    assert doc["state"] == ("partial" if with_blank_page else "ready"), doc.get("error")
    assert doc["ir"]["parser"] == "docling/native"
    assert doc["parse_revision"] == 1 and doc["ir_fingerprint"] == ir_fingerprint(doc["ir"])
    drain(app, owner)
    found = search(client, owner, "Native PDF evidence")
    assert found["total"] == 1
    hit = found["items"][0]
    assert hit["complete"] is not with_blank_page
    assert hit["status"] == doc["state"] and hit["warnings"]
    result = assert_bound_download(client, owner, hit, doc, original)
    evidence = result["unit"]["evidence"]
    assert evidence["page_number"] == (2 if with_blank_page else 1)
    assert evidence["coordinate_system"] == "pdf_points_BOTTOMLEFT"
    assert evidence["bbox"] and result["unit"]["provenance"][0] == evidence
    assert result["coverage"]["complete"] is not with_blank_page
    if with_blank_page:
        assert hit["pages"][0]["status"] == "needs_ocr"
        assert hit["pages"][1]["status"] == "parsed"
        assert hit["quality"] == "needs_review"
    else:
        assert hit["pages"][0]["status"] == "parsed"
    assert (
        client.get(
            f"/api/documents/{doc['id']}/download",
            params={
                "expected_revision": 2,
                "expected_hash": doc["content_hash"],
            },
        ).status_code
        == 409
    )


def test_tampered_uploaded_bytes_fail_worker_then_retry_restores_bound_search(platform):
    client, app, owner, _ = platform
    original = "# 保养记录\n\n阀门检查完成。".encode()
    uploaded = upload(client, owner["project_id"], original, "保养记录.md")
    doc_id = uploaded["document"]["id"]
    with app.state.db.transaction(owner["tenant_id"]) as session:
        doc = session.get(Document, doc_id)
        asset = app.state.settings.data_dir / "assets" / doc.storage_key
    # Only this fixture's own temporary uploaded file is changed. Both payloads
    # are valid Markdown, so the failure tests identity binding, not parser syntax.
    asset.write_bytes("# 伪造内容\n\n未经验证的窜改记录。".encode())
    assert process_one(app.state.db, app.state.settings)
    failed = client.get(f"/api/documents/{doc_id}").json()
    assert failed["state"] == "failed"
    assert failed["ir"] == {} and failed["ir_fingerprint"] is None and failed["parse_revision"] == 0
    with app.state.db.transaction(owner["tenant_id"]) as session:
        assert session.get(Job, uploaded["job"]["id"]).status == "failed"
    drain(app, owner)
    assert search(client, owner, "窜改")["total"] == 0
    assert search(client, owner, "阀门")["total"] == 0
    assert (
        client.get(
            f"/api/documents/{doc_id}/download",
            params={
                "expected_revision": 1,
                "expected_hash": hashlib.sha256(original).hexdigest(),
            },
        ).status_code
        == 409
    )

    asset.write_bytes(original)
    retry = client.post(f"/api/jobs/{uploaded['job']['id']}/retry")
    assert retry.status_code == 200, retry.text
    assert process_one(app.state.db, app.state.settings)
    restored = client.get(f"/api/documents/{doc_id}").json()
    assert restored["state"] == "ready" and restored["parse_revision"] == 1
    assert restored["ir_fingerprint"] == ir_fingerprint(restored["ir"])
    drain(app, owner)
    hit = search(client, owner, "阀门")["items"][0]
    assert_bound_download(client, owner, hit, restored, original)
