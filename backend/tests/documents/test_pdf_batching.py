import hashlib
import io
import json
import subprocess
from pathlib import Path

import pytest
from ontology import worker
from ontology.core.errors import DomainError
from ontology.documents import DocumentError, ParseLimits, parse_batches, parse_document
from ontology.documents.pdf_native import parse_pdf_batch
from ontology.documents.storage_models import Document
from ontology.jobs.models import Job
from ontology.jobs.service import claim
from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

from tests.support.content import upload


def native_pdf(pages, *, blank_page=None):
    writer = PdfWriter()
    font = DictionaryObject(
        {
            NameObject("/Type"): NameObject("/Font"),
            NameObject("/Subtype"): NameObject("/Type1"),
            NameObject("/BaseFont"): NameObject("/Helvetica"),
        }
    )
    for number in range(1, pages + 1):
        page = writer.add_blank_page(width=600, height=800)
        if number != blank_page:
            page[NameObject("/Resources")] = DictionaryObject(
                {NameObject("/Font"): DictionaryObject({NameObject("/F1"): font})}
            )
            stream = DecodedStreamObject()
            # Vector drawing operations should not trigger the former Python text-stream bottleneck.
            drawing = b"10 20 m 40 50 l S\n" * 100
            stream.set_data(drawing + f"BT /F1 12 Tf 30 700 Td (Synthetic page {number}) Tj ET".encode())
            page[NameObject("/Contents")] = writer._add_object(stream)
    output = io.BytesIO()
    writer.write(output)
    return output.getvalue()


def test_native_pdfium_covers_404_pages_without_python_stream_extraction(monkeypatch):
    from pypdf._page import PageObject

    def forbidden(*_args, **_kwargs):
        raise AssertionError("Native PDF must not use the Python content-stream extractor")

    monkeypatch.setattr(PageObject, "extract_text", forbidden)
    data = native_pdf(404, blank_page=201)
    ir = parse_document(data, "synthetic.pdf", source_id="synthetic", source_revision="1")
    assert len(ir.pages) == 404 and len(ir.elements) == 404
    assert "Synthetic page 404" in ir.elements[-1].text
    assert ir.pages[200].status == "needs_ocr" and not ir.complete
    assert ir.parser_version.startswith("pdfium/")
    assert all(page.page_number == index for index, page in enumerate(ir.pages, 1))


def test_native_batch_opens_path_without_whole_file_copy_and_honors_page_limit(tmp_path, monkeypatch):
    data = native_pdf(35)
    path = tmp_path / "synthetic.pdf"
    path.write_bytes(data)
    original = Path.read_bytes

    def no_source_copy(self):
        if self == path:
            raise AssertionError("The source PDF must be opened by PDFium, not read_bytes")
        return original(self)

    monkeypatch.setattr(Path, "read_bytes", no_source_copy)
    result = parse_pdf_batch(
        path,
        path.name,
        source_id="source",
        source_revision="1",
        content_hash=hashlib.sha256(data).hexdigest(),
        start_page=33,
    )
    assert result["page_count"] == 35 and result["next_page"] is None
    assert [page["page_number"] for page in result["ir"]["pages"]] == [33, 34, 35]
    with pytest.raises(DocumentError) as caught:
        parse_pdf_batch(
            path,
            path.name,
            source_id="source",
            source_revision="1",
            content_hash="hash",
            limits=ParseLimits(max_pages=34),
        )
    assert caught.value.code == "resource_limit"


@pytest.mark.parametrize("interruption", ["timeout", "cancel"])
def test_worker_pdf_resume_persists_only_batch_references_and_commits_complete_scope(
    platform, monkeypatch, interruption
):
    client, app, owner, _ = platform
    created = upload(client, owner["project_id"], native_pdf(70, blank_page=35), "large-synthetic.pdf")
    job_id = created["job"]["id"]
    original = worker.execute_parse
    calls = []
    interrupted = False

    def execute(request, settings, environment):
        nonlocal interrupted
        start = request["pdf_batch"]["start_page"]
        calls.append(start)
        if start == 33 and not interrupted:
            interrupted = True
            if interruption == "timeout":
                raise subprocess.TimeoutExpired("synthetic-parser", 180)
            result = original(request, settings, environment)
            assert client.post(f"/api/jobs/{job_id}/cancel").status_code == 200
            return result
        return original(request, settings, environment)

    monkeypatch.setattr(worker, "execute_parse", execute)
    assert worker.process_one(app.state.db, app.state.settings)
    with app.state.db.transaction(owner["tenant_id"]) as session:
        job = session.get(Job, job_id)
        assert job.status == ("failed" if interruption == "timeout" else "cancelled")
        checkpoint = job.checkpoint["native_pdf"]
        assert checkpoint["pages_completed"] == 32
        assert "Synthetic page" not in json.dumps(checkpoint)
        assert len(json.dumps(checkpoint)) < 1000
        assert session.get(Document, created["document"]["id"]).parse_revision == 0
    assert client.post(f"/api/jobs/{job_id}/retry").status_code == 200
    assert worker.process_one(app.state.db, app.state.settings)
    assert calls == [1, 33, 33, 65]
    result = client.get(f"/api/documents/{created['document']['id']}").json()
    assert result["parse_revision"] == 1 and result["state"] == "partial"
    assert [page["page_number"] for page in result["ir"]["pages"]] == list(range(1, 71))
    assert [element["order"] for element in result["ir"]["elements"]] == list(range(70))
    assert "Synthetic page 70" in result["ir"]["elements"][-1]["text"]
    assert not (app.state.settings.data_dir / "temporary" / ("pdf-" + job_id)).exists()


def test_global_character_budget_is_not_reset_for_each_pdf_batch(platform, monkeypatch):
    client, app, owner, _ = platform
    created = upload(client, owner["project_id"], native_pdf(3), "synthetic.pdf")
    run = claim(app.state.db, "budget-test", kinds=["document.parse"])
    with app.state.db.transaction(owner["tenant_id"]) as session:
        doc = session.get(Document, created["document"]["id"])
        request = {
            "path": str(app.state.settings.data_dir / "assets" / doc.storage_key),
            "filename": doc.filename,
            "document_id": doc.id,
            "revision": doc.revision,
            "content_hash": doc.content_hash,
            "limits": {"max_characters": 30},
            "engine": "basic",
        }
    monkeypatch.setattr(parse_batches, "PDF_BATCH_PAGES", 1)

    def execute(batch):
        return parse_pdf_batch(
            Path(batch["path"]),
            batch["filename"],
            source_id=batch["document_id"],
            source_revision=str(batch["revision"]),
            content_hash=batch["content_hash"],
            **batch["pdf_batch"],
            limits=ParseLimits.model_validate(batch["limits"]),
        )

    with pytest.raises(DomainError) as caught:
        parse_batches.parse_pdf_batches(app.state.db, app.state.settings, run, request, execute)
    assert caught.value.code == "resource_limit"
    with app.state.db.transaction(owner["tenant_id"]) as session:
        assert session.get(Job, run["id"]).checkpoint["pages_completed"] == 1
        assert session.get(Document, doc.id).parse_revision == 0
