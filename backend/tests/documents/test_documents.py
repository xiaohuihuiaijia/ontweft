from __future__ import annotations

import hashlib
import io
import zipfile
from decimal import Decimal

import pytest
from ontology.documents import DocumentError, ParseLimits, detect_format

from tests.support.documents import _native_pdf_sample, parse, word


def test_word_reading_order_merges_and_structural_evidence():
    data = word("""<w:p><w:pPr><w:pStyle w:val="Heading1"/></w:pPr><w:r><w:t>项目知识</w:t></w:r></w:p>
    <w:tbl><w:tr><w:tc><w:tcPr><w:gridSpan w:val="2"/><w:vMerge w:val="restart"/></w:tcPr><w:p><w:r><w:t>合并</w:t></w:r></w:p></w:tc><w:tc><w:p><w:r><w:t>A</w:t></w:r></w:p></w:tc></w:tr>
    <w:tr><w:tc><w:tcPr><w:gridSpan w:val="2"/><w:vMerge/></w:tcPr><w:p/></w:tc><w:tc><w:p><w:r><w:t>B</w:t></w:r></w:p></w:tc></w:tr></w:tbl>
    <w:p><w:r><w:t>后续正文</w:t></w:r></w:p>""")
    ir = parse(data, "中文.docx")
    assert [e.kind for e in ir.elements] == ["heading", "table", "paragraph"]
    table = ir.elements[1]
    assert (table.cells[0].row_span, table.cells[0].column_span) == (2, 2)
    assert table.rows == 2 and table.columns == 3
    assert table.cells[0].evidence.element_path.endswith("/row/0/cell/0")
    assert all(e.evidence.page_number is None for e in ir.elements)
    assert ir.content_hash == hashlib.sha256(data).hexdigest()
    assert ir.elements[-1].text == "后续正文"
    assert parse(data, "renamed.docx").elements[0].element_id == ir.elements[0].element_id


def test_word_image_budget_does_not_cut_off_later_text():
    data = word(
        '<w:p><w:r><w:drawing><a:blip r:embed="rId1"/></w:drawing></w:r></w:p><w:p><w:r><w:t>图片之后仍有正文</w:t></w:r></w:p>',
        {
            "word/_rels/document.xml.rels": b'<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Target="media/image.png" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/image"/></Relationships>',
            "word/media/image.png": b"image-content-not-decoded-by-this-path",
        },
    )
    ir = parse(data, "images.docx", limits=ParseLimits(max_images=0))
    assert "图片之后仍有正文" in ir.text
    assert any("Image budget" in warning for warning in ir.warnings)
    assert not any(element.kind == "image" for element in ir.elements)
    assert not ir.complete and ir.quality == "needs_review"


def test_xml_entity_zip_bomb_mismatch_and_legacy_formats_are_explicit():
    bad = io.BytesIO()
    with zipfile.ZipFile(bad, "w") as archive:
        archive.writestr("word/document.xml", '<!DOCTYPE x [<!ENTITY p SYSTEM "file:///secret">]><x>&p;</x>')
    with pytest.raises(DocumentError) as exc:
        parse(bad.getvalue(), "entity.docx")
    assert exc.value.code == "invalid_xml"
    data = word("<w:p><w:r><w:t>" + "a" * 20000 + "</w:t></w:r></w:p>")
    with pytest.raises(DocumentError) as exc:
        parse(data, "large.docx", limits=ParseLimits(max_expanded_bytes=1000))
    assert exc.value.code == "resource_limit"
    with pytest.raises(DocumentError) as exc:
        detect_format(b"%PDF-1.4\n", "fake.docx")
    assert exc.value.code == "format_mismatch"
    with pytest.raises(DocumentError) as exc:
        detect_format(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1", "old.doc")
    assert exc.value.code == "conversion_required"


def test_pdf_tracks_each_page_and_does_not_invent_bounding_boxes():
    from pypdf import PdfWriter
    from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

    writer = PdfWriter()
    writer.add_blank_page(width=612, height=792)
    page = writer.add_blank_page(width=612, height=792)
    font = DictionaryObject(
        {
            NameObject("/Type"): NameObject("/Font"),
            NameObject("/Subtype"): NameObject("/Type1"),
            NameObject("/BaseFont"): NameObject("/Helvetica"),
        }
    )
    page[NameObject("/Resources")] = DictionaryObject(
        {NameObject("/Font"): DictionaryObject({NameObject("/F1"): font})}
    )
    stream = DecodedStreamObject()
    stream.set_data(b"BT /F1 12 Tf 30 700 Td (Later page text) Tj ET")
    page[NameObject("/Contents")] = writer._add_object(stream)
    output = io.BytesIO()
    writer.write(output)
    ir = parse(output.getvalue(), "two-pages.pdf")
    assert [p.status for p in ir.pages] == ["needs_ocr", "parsed"]
    assert "Later page text" in ir.text
    assert ir.elements[1].evidence.page_number == 2
    assert ir.elements[1].evidence.bbox is None
    assert not ir.complete and ir.quality == "needs_review"
    encrypted = PdfWriter()
    encrypted.add_blank_page(width=10, height=10)
    encrypted.encrypt("password")
    output = io.BytesIO()
    encrypted.write(output)
    with pytest.raises(DocumentError) as exc:
        parse(output.getvalue(), "encrypted.pdf")
    assert exc.value.code == "encrypted"


def test_spreadsheet_formula_cache_merged_cells_and_addresses():
    from openpyxl import Workbook

    book = Workbook()
    sheet = book.active
    sheet.title = "项目"
    sheet["A1"] = "合并标题"
    sheet.merge_cells("A1:B2")
    sheet["C1"] = 10
    sheet["C2"] = "=C1*2"
    output = io.BytesIO()
    book.save(output)
    book.close()
    ir = parse(output.getvalue(), "项目.xlsx")
    cells = {c.evidence.cell_address: c for c in ir.elements[0].cells}
    assert (cells["A1"].row_span, cells["A1"].column_span) == (2, 2)
    assert cells["C1"].value == 10
    assert cells["C2"].formula == "=C1*2"
    assert cells["C2"].cached_value is None and cells["C2"].value is None
    assert cells["C2"].evidence.sheet == "项目"


def test_csv_quoted_newline_json_precision_and_markdown_ranges():
    csv_ir = parse('name,notes\r\n"项目","line1\nline2"\r\n'.encode(), "rows.csv")
    assert csv_ir.elements[0].rows == 2
    assert csv_ir.elements[0].cells[-1].value == "line1\nline2"
    json_ir = parse(b'{"v":1.1234567890123456789,"nil":null,"array":[1,{"x":2}]}', "data.json")
    typed = json_ir.elements[0].metadata["value"]
    assert typed["v"] == Decimal("1.1234567890123456789") and typed["nil"] is None
    assert "missing" not in typed
    md = parse("# 标题\n- 条目\n正文".encode(), "readme.md")
    assert [e.kind for e in md.elements] == ["heading", "list_item", "paragraph"]
    assert md.elements[-1].evidence.char_start == len("# 标题\n- 条目\n")
    assert md.elements[-1].evidence.line_start == 3
    for invalid in [b'{"x":1,"x":2}', b'{"x":NaN}']:
        with pytest.raises(DocumentError) as exc:
            parse(invalid, "bad.json")
        assert exc.value.code == "invalid_json"


def test_budget_and_structured_engine_are_not_silent_fallbacks():
    with pytest.raises(DocumentError) as exc:
        parse(b"abcdef", "text.txt", limits=ParseLimits(max_bytes=2))
    assert exc.value.code == "resource_limit"
    with pytest.raises(DocumentError) as exc:
        parse(b"line1\nline2", "text.txt", limits=ParseLimits(max_elements=1))
    assert exc.value.code == "resource_limit"
    with pytest.raises(DocumentError) as exc:
        parse(b"plain text", "text.txt", engine="docling")
    assert exc.value.code in {"parser_unavailable", "parser_not_configured", "unsupported_format"}


def test_docling_real_word_preserves_chinese_merges_and_no_fake_page_numbers():
    pytest.importorskip("docling")
    from docx import Document

    source = Document()
    source.add_heading("项目知识", level=1)
    source.add_paragraph("原生中文正文")
    table = source.add_table(rows=2, cols=2)
    table.cell(0, 0).merge(table.cell(1, 0))
    table.cell(0, 0).text = "跨行合并"
    table.cell(0, 1).text = "A"
    table.cell(1, 1).text = "B"
    source.add_paragraph("末尾证据")
    output = io.BytesIO()
    source.save(output)
    ir = parse(output.getvalue(), "真实文档.docx", engine="docling")
    assert ir.parser == "docling/docx" and ir.parser_version == "2.143.1"
    assert "原生中文正文" in ir.text and "末尾证据" in ir.text
    table_ir = next(e for e in ir.elements if e.kind == "table")
    assert table_ir.cells[0].row_span == 2
    assert table_ir.cells[0].evidence.element_path.startswith("docling/#/tables/")
    assert all(e.evidence.page_number is None for e in ir.elements)
    assert ir.pages == [] and ir.quality == "structured"


def test_docling_real_xlsx_retains_native_formula_evidence_layer():
    pytest.importorskip("docling")
    from openpyxl import Workbook

    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "Project"
    sheet.append(["name", "amount"])
    sheet.append(["demo", 3])
    sheet["B3"] = "=B2*2"
    output = io.BytesIO()
    workbook.save(output)
    ir = parse(output.getvalue(), "real.xlsx", engine="docling")
    assert ir.parser == "docling/xlsx"
    assert any(element.kind == "table" for element in ir.elements)
    native_cells = {c.evidence.cell_address: c for e in ir.source_elements for c in e.cells}
    assert native_cells["B3"].formula == "=B2*2"
    assert native_cells["B3"].evidence.sheet == "Project"
    assert native_cells["B3"].cached_value is None


def test_docling_real_native_pdf_outputs_actual_page_boxes(monkeypatch):
    pytest.importorskip("docling")
    pytest.importorskip("pypdfium2")
    monkeypatch.setenv("ONTOLOGY_DOCLING_PDF_MODE", "native")
    ir = parse(_native_pdf_sample(), "native.pdf", engine="docling")
    assert ir.parser == "docling/native" and ir.quality == "basic"
    assert "Native PDF evidence" in ir.text
    element = next(e for e in ir.elements if "Native PDF evidence" in e.text)
    assert element.evidence.page_number == 1 and element.evidence.bbox is not None
    assert element.evidence.coordinate_system == "pdf_points_BOTTOMLEFT"
    assert element.provenance[0] == element.evidence
    assert ir.pages[0].status == "parsed"
    assert ir.pages[0].width == 612 and ir.pages[0].height == 792


def test_docling_layout_missing_bundle_does_not_fall_back_or_download(monkeypatch):
    pytest.importorskip("docling")
    monkeypatch.setenv("ONTOLOGY_DOCLING_PDF_MODE", "layout")
    monkeypatch.delenv("ONTOLOGY_DOCLING_ARTIFACTS_PATH", raising=False)
    monkeypatch.delenv("ONTOLOGY_DOCLING_MANIFEST_SHA256", raising=False)
    with pytest.raises(DocumentError) as exc:
        parse(_native_pdf_sample(), "layout.pdf", engine="docling")
    assert exc.value.code == "parser_not_configured"


def test_docling_offline_guard_blocks_sockets_and_restores_environment(monkeypatch):
    import os
    import socket

    from ontology.documents.structured import offline_docling

    original = socket.create_connection
    monkeypatch.setenv("HF_HUB_OFFLINE", "0")
    with offline_docling():
        assert os.environ["HF_HUB_OFFLINE"] == "1"
        with pytest.raises(DocumentError) as exc:
            socket.create_connection(("example.com", 443))
        assert exc.value.code == "network_disabled"
    assert socket.create_connection is original and os.environ["HF_HUB_OFFLINE"] == "0"


def test_docling_bundle_manifest_rejects_missing_tampered_and_unlisted_files(tmp_path):
    import json

    from ontology.documents.structured import DoclingOptions, _validate_artifacts

    model = tmp_path / "model.bin"
    model.write_bytes(b"known-weight")
    manifest = tmp_path / "manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "docling_version": "2.143.1",
                "files": {"model.bin": hashlib.sha256(model.read_bytes()).hexdigest()},
            }
        )
    )
    options = DoclingOptions(
        artifacts_path=tmp_path, artifacts_manifest_sha256=hashlib.sha256(manifest.read_bytes()).hexdigest()
    )
    assert _validate_artifacts(options) == tmp_path
    model.write_bytes(b"tampered")
    with pytest.raises(DocumentError) as exc:
        _validate_artifacts(options)
    assert exc.value.code == "artifact_integrity"


def test_docling_xlsx_interpretation_and_source_share_one_budget():
    pytest.importorskip("docling")
    from openpyxl import Workbook

    workbook = Workbook()
    workbook.active.append(["name", "amount"])
    workbook.active.append(["demo", 3])
    output = io.BytesIO()
    workbook.save(output)
    workbook.close()
    with pytest.raises(DocumentError) as exc:
        parse(output.getvalue(), "bounded.xlsx", engine="docling", limits=ParseLimits(max_cells=6))
    assert exc.value.code == "resource_limit"


def test_docling_multipage_table_never_guesses_cell_page():
    pytest.importorskip("docling")
    from docling_core.types.doc import BoundingBox, DoclingDocument, ProvenanceItem, TableCell, TableData
    from ontology.documents.structured import _document_ir

    document = DoclingDocument(name="table-mapping-contract")
    bbox = BoundingBox(l=10, t=20, r=30, b=40)
    table = document.add_table(
        data=TableData(
            num_rows=1,
            num_cols=1,
            table_cells=[
                TableCell(
                    bbox=bbox,
                    start_row_offset_idx=0,
                    end_row_offset_idx=1,
                    start_col_offset_idx=0,
                    end_col_offset_idx=1,
                    text="Unassigned cell page",
                )
            ],
        )
    )
    table.prov = [ProvenanceItem(page_no=page, bbox=bbox, charspan=(0, 20)) for page in (1, 2)]
    ir = _document_ir(
        document,
        data=b"table-reference",
        filename="reference.pdf",
        fmt="pdf",
        limits=ParseLimits(),
        source_id="source",
        source_revision="revision",
        mode="layout",
        status="success",
        page_statuses=[],
        errors=[],
    )
    element = ir.elements[0]
    assert [ref.page_number for ref in element.provenance] == [1, 2]
    assert element.cells[0].evidence.page_number is None
    assert element.cells[0].evidence.bbox is None
    assert element.cells[0].evidence.char_start is None
    assert element.metadata["cell_page_mapping"] == "unavailable"
