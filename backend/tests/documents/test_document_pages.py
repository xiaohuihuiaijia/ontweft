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
