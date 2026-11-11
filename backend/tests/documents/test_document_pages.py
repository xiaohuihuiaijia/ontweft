import base64
import io
import zipfile

import pytest
from ontology.documents import DocumentError, parse_document
from ontology.documents.pages import iter_page_inputs, page_count, page_image
from ontology.documents.parser import detect_format_path
from PIL import Image

from tests.support.documents import _native_pdf_sample


def png():
    out = io.BytesIO()
    Image.new("RGB", (400, 200), "#236c56").save(out, "PNG")
    return out.getvalue()


def presentation():
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as archive:
        archive.writestr(
            "ppt/presentation.xml",
            '<p:presentation xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><p:sldIdLst><p:sldId id="256" r:id="slide1"/></p:sldIdLst></p:presentation>',
        )
        archive.writestr(
            "ppt/_rels/presentation.xml.rels",
            '<Relationships><Relationship Id="slide1" Target="slides/slide1.xml"/></Relationships>',
        )
        archive.writestr(
            "ppt/slides/slide1.xml",
            """<p:sld xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main" xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><p:cSld><p:spTree>
        <p:sp><p:nvSpPr><p:nvPr><p:ph type="title"/></p:nvPr></p:nvSpPr><p:txBody><a:p><a:r><a:t>产品参数</a:t></a:r></a:p></p:txBody></p:sp>
        <p:graphicFrame><a:graphic><a:graphicData><a:tbl><a:tblGrid><a:gridCol/><a:gridCol/></a:tblGrid><a:tr><a:tc gridSpan="2"><a:txBody><a:p><a:r><a:t>12 V，仅用于示例</a:t></a:r></a:p></a:txBody></a:tc><a:tc hMerge="1"/></a:tr></a:tbl></a:graphicData></a:graphic></p:graphicFrame>
        <p:pic><p:blipFill><a:blip r:embed="image1"/></p:blipFill></p:pic>
        </p:spTree></p:cSld></p:sld>""",
        )
        archive.writestr(
            "ppt/slides/_rels/slide1.xml.rels",
            '<Relationships><Relationship Id="image1" Target="../media/image1.png"/></Relationships>',
        )
        archive.writestr("ppt/media/image1.png", png())
    return out.getvalue()


def test_pptx_preserves_slides_merges_images_and_condition_text(tmp_path):
    data = presentation()
    path = tmp_path / "sample.pptx"
    path.write_bytes(data)
    assert detect_format_path(path, path.name) == "pptx"
    ir = parse_document(data, path.name, source_id="doc", source_revision="1")
    assert ir.elements[0].kind == "heading"
    table = next(e for e in ir.elements if e.kind == "table")
    assert table.cells[0].column_span == 2
    assert "仅用于示例" in table.cells[0].text
    assert table.cells[0].evidence.page_number == 1
    assert not ir.complete
    pages = list(
        iter_page_inputs(path, path.name, source_id="doc", source_revision="1", ir=ir.model_dump(mode="json"))
    )
    assert len(pages) == 1 and pages[0]["label"] == "幻灯片 1"
    assert "12 V" in pages[0]["text"] and len(pages[0]["image_data_urls"]) == 1
    assert (
        page_count(path, path.name, source_id="doc", source_revision="1", ir=ir.model_dump(mode="json")) == 1
    )


def test_pdf_pages_render_without_ocr_and_keep_exact_source_identity(tmp_path):
    path = tmp_path / "sample.pdf"
    path.write_bytes(_native_pdf_sample())
    assert page_count(path, path.name, source_id="doc", source_revision="1") == 1
    page = next(
        iter_page_inputs(
            path, path.name, source_id="doc", source_revision="1", page_numbers=[1], image_max_edge=512
        )
    )
    assert "Native PDF evidence" in page["text"]
    assert page["evidence"]["page_number"] == 1
    assert page["evidence"]["source_id"] == "doc"
    image = Image.open(io.BytesIO(base64.b64decode(page["image_data_url"].split(",")[1])))
    assert max(image.size) <= 513
    with pytest.raises(DocumentError, match="outside"):
        list(iter_page_inputs(path, path.name, source_id="doc", source_revision="1", page_numbers=[2]))


def test_png_native_parse_and_preview_are_explicitly_visual(tmp_path):
    path = tmp_path / "picture.png"
    path.write_bytes(png())
    ir = parse_document(path.read_bytes(), path.name, source_id="doc", source_revision="1")
    assert not ir.complete and ir.format == "png"
    assert ir.elements[0].metadata["width"] == 400
    image = page_image(path, path.name, source_id="doc", source_revision="1", page_number=1)
    assert image.startswith(b"\x89PNG")


def test_word_notes_and_workbook_embedded_picture_are_not_omitted(tmp_path):
    from openpyxl import Workbook
    from openpyxl.drawing.image import Image as SheetImage

    from tests.support.documents import word

    data = word(
        "<w:p><w:r><w:t>参数</w:t></w:r></w:p>",
        {
            "word/footnotes.xml": b'<w:footnotes xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:footnote w:id="1"><w:p><w:r><w:t>Only for rated temperature</w:t></w:r></w:p></w:footnote></w:footnotes>'
        },
    )
    ir = parse_document(data, "notes.docx", source_id="doc", source_revision="1")
    assert "Only for rated temperature" in ir.text
    assert ir.elements[-1].evidence.element_path.startswith("word/footnotes.xml/")
    book = Workbook()
    book.active.title = "Specifications"
    book.active["A1"] = "Rating"
    book.active["A2"] = "12 V"
    book.active.add_image(SheetImage(io.BytesIO(png())), "C1")
    path = tmp_path / "picture.xlsx"
    book.save(path)
    book.close()
    ir = parse_document(path.read_bytes(), path.name, source_id="doc", source_revision="1")
    picture = next(e for e in ir.elements if e.kind == "image")
    assert picture.evidence.sheet == "Specifications"
    assert not ir.complete
    pages = list(
        iter_page_inputs(path, path.name, source_id="doc", source_revision="1", ir=ir.model_dump(mode="json"))
    )
    assert any(page["image_data_urls"] for page in pages)


@pytest.mark.parametrize("with_image", [False, True])
def test_historical_docling_word_keeps_text_and_native_images_once_without_invented_pairing(tmp_path, with_image):
    import copy

    from ontology.documents.pages import page_structure

    from tests.support.documents import word

    body = "<w:p><w:r><w:t>Source condition</w:t></w:r></w:p>"
    extras = {}
    if with_image:
        body += '<w:p><w:r><w:drawing><a:blip r:embed="image1"/></w:drawing></w:r></w:p>'
        extras = {
            "word/_rels/document.xml.rels": b'<Relationships><Relationship Id="image1" Target="media/image.png"/></Relationships>',
            "word/media/image.png": png(),
        }
    data = word(body, extras)
    path = tmp_path / "synthetic.docx"
    path.write_bytes(data)
    ir = parse_document(data, path.name, source_id="doc", source_revision="1").model_dump(mode="json")
    assert ir["complete"] is not with_image
    # This is the retained Docling IR shape: textual interpretation and picture
    # metadata, without a verifiable native image/member correspondence.
    ir["parser"] = "docling/docx"
    for element in ir["elements"]:
        if element["kind"] == "image":
            element["metadata"] = {"docling_ref": "#/pictures/0"}
    previous = copy.deepcopy(ir)
    prepared = page_structure(path, path.name, source_id="doc", source_revision="1", ir=ir)
    assert ir == previous
    assert page_structure(path, path.name, source_id="doc", source_revision="1", ir=prepared) is prepared
    pages = list(iter_page_inputs(path, path.name, source_id="doc", source_revision="1", ir=prepared))
    assert page_count(path, path.name, source_id="doc", source_revision="1", ir=prepared) == len(pages)
    assert sum(page["text"].count("Source condition") for page in pages) == 1
    assert sum(len(page["image_data_urls"]) for page in pages) == int(with_image)
    if with_image:
        assert pages[-1]["label"] == "原生内嵌图像" and pages[-1]["status"] == "needs_vision"
        assert pages[-1]["evidence"]["element_path"].startswith("word/document.xml/")
        assert pages[-1]["evidence"]["page_number"] is None


def test_upload_is_chunked_and_oversize_partial_file_is_removed(platform, monkeypatch):
    from starlette.datastructures import UploadFile

    client, app, owner, _ = platform
    reads = []
    original = UploadFile.read

    async def recording_read(self, size=-1):
        reads.append(size)
        return await original(self, size)

    monkeypatch.setattr(UploadFile, "read", recording_read)
    response = client.post(
        "/api/documents/upload",
        data={"project_id": owner["project_id"]},
        files={"file": ("large.txt", b"a" * (2 * 1024 * 1024 + 7))},
    )
    assert response.status_code == 201, response.text
    assert len(reads) >= 3 and all(size == 1024 * 1024 for size in reads)
    assert response.json()["job"]["payload"]["engine"] == "basic"
    app.state.settings.max_upload_bytes = 10
    response = client.post(
        "/api/documents/upload",
        data={"project_id": owner["project_id"]},
        files={"file": ("too-large.txt", b"x" * 11)},
    )
    assert response.status_code == 413
    assert not list((app.state.settings.data_dir / "assets").rglob("upload.part"))


def test_page_endpoints_enforce_project_scope_and_do_not_expose_images_inline(platform):
    from tests.support.content import upload

    client, _, owner, _ = platform
    doc = upload(client, owner["project_id"], png(), "image.png")["document"]
    url = f"/api/documents/{doc['id']}/pages"
    response = client.get(url)
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["total"] == 1 and data["items"][0]["has_image"]
    assert "image_data_url" not in data["items"][0]
    image_url = data["items"][0]["image_url"]
    image = client.get(image_url)
    assert image.status_code == 200 and image.headers["content-type"] == "image/png"
    assert "no-store" in image.headers["cache-control"]
    assert client.get(url + "?offset=1").json()["items"] == []
    client.post(
        "/api/auth/login",
        json={"tenant_slug": "beta", "email": "owner@example.com", "password": "Safe-Test-Password-2026"},
    )
    assert client.get(url).status_code == 404
    assert client.get(image_url).status_code == 404


def test_cached_logical_page_checks_the_actual_original_before_returning_evidence(platform):
    from ontology import worker
    from ontology.documents.storage_models import Document

    from tests.support.content import upload

    client, app, owner, _ = platform
    doc = upload(client, owner["project_id"], b"Original synthetic evidence", "evidence.txt")["document"]
    assert worker.process_one(app.state.db, app.state.settings)
    with app.state.db.transaction(owner["tenant_id"]) as session:
        path = app.state.settings.data_dir / "assets" / session.get(Document, doc["id"]).storage_key
    path.write_bytes(b"Changed synthetic evidence")
    response = client.get(f"/api/documents/{doc['id']}/pages")
    assert response.status_code == 409
    assert "Original synthetic" not in response.text


def test_page_image_hashes_original_once_and_respects_configured_upload_limit(platform, monkeypatch):
    from ontology.documents import pages

    from tests.support.content import upload

    client, app, owner, _ = platform
    doc = upload(client, owner["project_id"], png(), "image.png")["document"]
    original, hashes = pages.file_digest, []

    def digest(path):
        hashes.append(path)
        return original(path)

    monkeypatch.setattr(pages, "file_digest", digest)
    url = f"/api/documents/{doc['id']}/pages/1/image"
    assert client.get(url).status_code == 200
    assert len(hashes) == 1
    app.state.settings.max_upload_bytes = 10
    assert client.get(url).status_code == 422
    assert client.get(f"/api/documents/{doc['id']}/pages").status_code == 422


def test_historical_page_image_uses_selected_parse_and_never_another_document_snapshot(platform):
    import copy

    from ontology import worker
    from ontology.documents.history import capture_current_parse
    from ontology.documents.storage_models import Document

    from tests.support.content import upload
    from tests.support.documents import word

    client, app, owner, _ = platform
    source = word('<w:p><w:r><w:drawing><a:blip r:embed="image1"/></w:drawing></w:r></w:p>', {
        "word/_rels/document.xml.rels": b'<Relationships><Relationship Id="image1" Target="media/image.png"/></Relationships>',
        "word/media/image.png": png(),
    })
    doc = upload(client, owner["project_id"], source, "synthetic.docx")["document"]
    assert worker.process_one(app.state.db, app.state.settings)
    detail = client.get(f"/api/documents/{doc['id']}").json()
    locator = detail["current_parse_id"]
    with app.state.db.transaction(owner["tenant_id"], write=True) as session:
        stored = session.get(Document, doc["id"])
        changed = copy.deepcopy(stored.ir)
        changed["elements"] = [e for e in changed["elements"] if e["kind"] != "image"]
        stored.ir, stored.ir_fingerprint = changed, None
        stored.parse_revision += 1
        capture_current_parse(session, stored)
    pages = client.get(f"/api/documents/{doc['id']}/pages", params={"native_parse_id": locator}).json()
    image_url = next(page["image_url"] for page in pages["items"] if page["image_url"])
    assert "native_parse_id=" + locator in image_url
    assert client.get(image_url).status_code == 200
    assert client.get(f"/api/documents/{doc['id']}/pages/1/image").status_code == 422
    other = upload(client, owner["project_id"], b"Another original", "other.txt")["document"]
    for suffix in ("/pages", "/pages/1/image"):
        assert client.get(f"/api/documents/{other['id']}" + suffix, params={"native_parse_id": locator}).status_code == 404
