from __future__ import annotations

from copy import deepcopy
from types import SimpleNamespace

import pytest
from ontology.documents import DocumentError, ParseLimits
from ontology.search.content import units


def ref(path="text/line/1", **changes):
    return {
        "source_id": "doc-1",
        "source_revision": "3",
        "content_hash": "a" * 64,
        "element_path": path,
        **changes,
    }


def element(text="维修计划", **changes):
    return {
        "element_id": "same-content-path-id",
        "kind": "paragraph",
        "order": 0,
        "text": text,
        "evidence": ref(),
        **changes,
    }


def document(elements=None, **changes):
    ir = {
        "source_id": "doc-1",
        "source_revision": "3",
        "content_hash": "a" * 64,
        "filename": "原件.txt",
        "format": "text",
        "parser": "basic/text",
        "parser_version": "1.0",
        "elements": elements if elements is not None else [element()],
        **changes,
    }
    return SimpleNamespace(id="doc-1", revision=3, content_hash="a" * 64, ir=ir)


def table(text="汇总文本不应重复索引", **changes):
    return element(
        text,
        kind="table",
        evidence=ref("table/0"),
        cells=[
            {
                "row": 0,
                "column": 0,
                "row_span": 2,
                "column_span": 2,
                "text": "合并单元格",
                "evidence": ref("table/0/cell/0"),
            },
            {"row": 0, "column": 2, "text": "重复内容", "evidence": ref("table/0/cell/1")},
            {"row": 1, "column": 2, "text": "重复内容", "evidence": ref("table/0/cell/2")},
        ],
        **changes,
    )


def assert_error(code, call):
    with pytest.raises(DocumentError) as caught:
        call()
    assert caught.value.code == code


def test_ir_fingerprint_covers_full_json_and_ignores_object_key_order_only():
    left = document().ir
    right = dict(reversed(list(left.items())))
    assert units.ir_fingerprint(left) == units.ir_fingerprint(right)
    for changed in [
        {**left, "parser_version": "2.0"},
        {**left, "unknown_future_metadata": {"profile": "layout"}},
        {**left, "elements": [element(" 维修计划")]},
        {**left, "elements": [element(evidence=ref("different/anchor"))]},
    ]:
        assert units.ir_fingerprint(left) != units.ir_fingerprint(changed)


@pytest.mark.parametrize(
    "value", [float("nan"), float("inf"), {1: "not-json-key"}, ("tuple",), object(), "\ud800"]
)
def test_fingerprint_rejects_non_json_or_invalid_unicode_without_silent_conversion(value):
    assert_error("invalid_document_ir", lambda: units.ir_fingerprint({"metadata": value}))


def test_fingerprint_rejects_cycle():
    value = {}
    value["cycle"] = value
    assert_error("invalid_document_ir", lambda: units.ir_fingerprint(value))


@pytest.mark.parametrize("location", ["root", "element", "provenance", "cell", "source_element"])
@pytest.mark.parametrize(
    "field,value", [("source_id", "other-doc"), ("source_revision", "4"), ("content_hash", "b" * 64)]
)
def test_all_evidence_layers_reject_different_source_identity(location, field, value):
    doc = document(
        [table(provenance=[ref("table/0", page_number=1)])],
        format="xlsx",
        source_elements=[element("原生表格")],
    )
    paths = {
        "root": doc.ir,
        "element": doc.ir["elements"][0]["evidence"],
        "provenance": doc.ir["elements"][0]["provenance"][0],
        "cell": doc.ir["elements"][0]["cells"][0]["evidence"],
        "source_element": doc.ir["source_elements"][0]["evidence"],
    }
    paths[location][field] = value
    # XLSX chooses the source layer for indexing but still validates both layers.
    assert_error("invalid_evidence", lambda: list(units.document_chunks(doc)))


def test_duplicate_content_positions_remain_distinct_and_original_whitespace_is_retained():
    doc = document([element(" 维修计划\n"), element(" 维修计划\n", order=1)])
    before = deepcopy(doc.ir)
    chunks = list(units.document_chunks(doc))
    assert [chunk["text"] for chunk in chunks] == [" 维修计划\n", " 维修计划\n"]
    assert [chunk["anchor"]["element_index"] for chunk in chunks] == [0, 1]
    assert chunks[0]["anchor"]["element_id"] == chunks[1]["anchor"]["element_id"]
    assert doc.ir == before


def test_long_unicode_units_round_trip_with_exact_overlap_and_unmodified_source_evidence():
    text = "中😀e\u0301" * 1600
    evidence = ref(char_start=90, char_end=90 + len(text))
    doc = document([element(text, kind="code", evidence=evidence)])
    chunks = list(units.document_chunks(doc))
    assert [chunk["anchor"]["unit_text_start"] for chunk in chunks] == [0, 1792, 3584, 5376]
    assert [len(chunk["text"]) for chunk in chunks] == [2048, 2048, 2048, 1024]
    rebuilt = chunks[0]["text"] + "".join(chunk["text"][256:] for chunk in chunks[1:])
    assert rebuilt == text
    for chunk in chunks:
        resolved = units.resolve_document_anchor(doc, chunk["anchor"])
        assert resolved["text"] == text and resolved["chunk_text"] == chunk["text"]
        assert resolved["evidence"]["char_start"] == 90
        assert resolved["evidence"]["char_end"] == 90 + len(text)
        assert resolved["evidence"]["bbox"] is None


def test_partial_keeps_extracted_text_without_inventing_empty_page_content():
    doc = document(
        [
            element("", evidence=ref("pdf/page/1", page_number=1)),
            element("第二页已提取的维修记录", order=1, evidence=ref("pdf/page/2", page_number=2)),
        ],
        format="pdf",
        complete=False,
        quality="needs_review",
        warnings=["OCR unavailable"],
        pages=[{"page_number": 1, "status": "needs_ocr"}, {"page_number": 2, "status": "parsed"}],
    )
    chunks = list(units.document_chunks(doc))
    assert len(chunks) == 1 and "第二页" in chunks[0]["text"]
    resolved = units.resolve_document_anchor(doc, chunks[0]["anchor"])
    assert resolved["evidence"]["page_number"] == 2 and resolved["evidence"]["bbox"] is None
    assert not units.validate_document_ir(doc).complete


def test_table_indexes_cells_once_keeps_merges_and_parent_provenance_distinct():
    doc = document(
        [
            table(
                provenance=[ref("table/0", page_number=1), ref("table/0", page_number=2)],
                metadata={"cell_page_mapping": "unavailable"},
            )
        ]
    )
    chunks = list(units.document_chunks(doc))
    assert [chunk["text"] for chunk in chunks] == ["合并单元格", "重复内容", "重复内容"]
    assert [chunk["anchor"]["cell_index"] for chunk in chunks] == [0, 1, 2]
    first = units.resolve_document_anchor(doc, chunks[0]["anchor"])
    assert first["metadata"]["table_cell"]["row_span"] == 2
    assert first["metadata"]["table_cell"]["column_span"] == 2
    assert first["evidence"]["page_number"] is None and first["evidence"]["bbox"] is None
    assert [reference["page_number"] for reference in first["provenance"]] == [1, 2]
    assert first["metadata"]["provenance_scope"] == "parent_table"
    first["metadata"]["cell_page_mapping"] = "changed by caller"
    assert doc.ir["elements"][0]["metadata"]["cell_page_mapping"] == "unavailable"


def test_xlsx_prefers_native_cells_and_preserves_formula_as_source_text():
    native = table()
    native["cells"][0].update(text="=SUM(C1:C2)", formula="=SUM(C1:C2)", cached_value=None)
    doc = document([element("错误的解释层表格")], format="xlsx", source_elements=[native])
    chunks = list(units.document_chunks(doc))
    assert all(chunk["anchor"]["layer"] == "source_elements" for chunk in chunks)
    assert chunks[0]["text"] == "=SUM(C1:C2)"
    assert all("错误" not in chunk["text"] for chunk in chunks)
    resolved = units.resolve_document_anchor(doc, chunks[0]["anchor"])
    assert resolved["metadata"]["table_cell"]["formula"] == "=SUM(C1:C2)"
    assert resolved["metadata"]["table_cell"]["cached_value"] is None


@pytest.mark.parametrize(
    "patch",
    [
        {"element_index": -1},
        {"element_index": True},
        {"element_id": "wrong"},
        {"cell_index": 0},
        {"unit_kind": "table_cell"},
        {"layer": "invented"},
        {"unit_text_start": -1},
        {"unit_text_start": True},
        {"unit_text_end": 100},
        {"unit_text_end": 0},
        {"unit_text_start": "0"},
        {"invented": "field"},
    ],
)
def test_invalid_document_anchor_is_rejected_not_clamped(patch):
    doc = document()
    anchor = next(iter(units.document_chunks(doc)))["anchor"]
    assert_error("invalid_anchor", lambda: units.resolve_document_anchor(doc, {**anchor, **patch}))


def test_table_aggregate_and_unselected_xlsx_layer_cannot_be_resolved_as_search_units():
    doc = document([table()], format="xlsx", source_elements=[table()])
    anchor = next(iter(units.document_chunks(doc)))["anchor"]
    assert_error("invalid_anchor", lambda: units.resolve_document_anchor(doc, {**anchor, "cell_index": None}))
    assert_error(
        "invalid_anchor", lambda: units.resolve_document_anchor(doc, {**anchor, "layer": "elements"})
    )


@pytest.mark.parametrize("budget", ["elements", "cells", "characters"])
def test_budgets_include_both_layers_and_fail_before_yielding_any_chunk(monkeypatch, budget):
    limits = ParseLimits(max_elements=10, max_cells=10, max_characters=100)
    if budget == "elements":
        limits.max_elements = 1
    elif budget == "cells":
        limits.max_cells = 2
    else:
        limits.max_characters = 8
    monkeypatch.setattr(units, "LIMITS", limits)
    doc = document([element("可见")], format="xlsx", source_elements=[table()])
    iterator = iter(units.document_chunks(doc))
    assert_error("resource_limit", lambda: next(iterator))


def test_cell_text_cannot_hide_behind_empty_table_summary_and_is_not_double_counted(monkeypatch):
    value = table("AB\nCD")
    value["cells"] = value["cells"][:2]
    value["cells"][0]["text"], value["cells"][1]["text"] = "AB", "CD"
    monkeypatch.setattr(units, "LIMITS", ParseLimits(max_characters=5))
    assert [x["text"] for x in units.document_chunks(document([value]))] == ["AB", "CD"]
    value["text"] = ""
    value["cells"][0]["text"] = "ABCDEF"
    assert_error("resource_limit", lambda: list(units.document_chunks(document([value]))))


def test_knowledge_title_body_positions_and_long_body_round_trip_without_deduplication():
    revision = SimpleNamespace(title="维修计划", body="维修计划\n" + "中文😀" * 1400)
    chunks = list(units.knowledge_chunks(revision))
    assert chunks[0]["anchor"] == {"field": "title", "unit_text_start": 0, "unit_text_end": 4}
    assert chunks[1]["anchor"]["field"] == "body"
    for chunk in chunks:
        resolved = units.resolve_knowledge_anchor(revision, chunk["anchor"])
        assert resolved["chunk_text"] == chunk["text"]
        assert resolved["text"] == getattr(revision, chunk["anchor"]["field"])
        assert resolved["evidence"] is None
    assert_error(
        "invalid_anchor",
        lambda: units.resolve_knowledge_anchor(
            revision,
            {
                "field": "references",
                "unit_text_start": 0,
                "unit_text_end": 1,
            },
        ),
    )


def test_knowledge_budget_rejects_entire_revision_before_returning_title(monkeypatch):
    monkeypatch.setattr(units, "LIMITS", ParseLimits(max_characters=5))
    revision = SimpleNamespace(title="title", body="over budget")
    assert_error("resource_limit", lambda: next(iter(units.knowledge_chunks(revision))))
