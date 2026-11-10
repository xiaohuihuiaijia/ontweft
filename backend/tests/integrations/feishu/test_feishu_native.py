"""Native cloud snapshots are versioned structures, not exported Word files."""

import copy
import hashlib
import json
from types import SimpleNamespace

import pytest
from ontology.documents import DocumentError, ParseLimits, detect_format
from ontology.documents.feishu_native import load_snapshot
from ontology.search.content.units import document_chunks, resolve_document_anchor

from tests.support.feishu_native import native_fixture, parsed, text_block


def test_reading_order_merge_evidence_and_search_units_are_bound_to_snapshot():
    data, ir = parsed()
    assert ir.complete and ir.quality == "structured" and ir.format == "feishu_docx"
    assert [e.kind for e in ir.elements] == ["heading", "heading", "paragraph", "table", "paragraph"]
    table = ir.elements[3]
    assert [(c.row, c.column, c.row_span, c.column_span, c.text) for c in table.cells] == [
        (0, 0, 2, 1, "阀门"),
        (0, 1, 1, 1, "季度检查"),
        (1, 1, 1, 1, "年度更换"),
    ]
    assert table.cells[0].evidence.element_path.endswith("/revisions/7/blocks/c00")
    assert table.cells[0].column_header and not table.cells[-1].column_header
    assert ir.content_hash == hashlib.sha256(data).hexdigest()
    assert all(e.evidence.page_number is None for e in ir.elements)
    doc = SimpleNamespace(
        id="platform-doc", revision=1, content_hash=ir.content_hash, ir=ir.model_dump(mode="json")
    )
    units = list(document_chunks(doc))
    assert len([u for u in units if u["text"] == "季度检查"]) == 1
    hit = next(u for u in units if u["text"] == "季度检查")
    resolved = resolve_document_anchor(doc, hit["anchor"])
    assert resolved["evidence"]["element_path"].endswith("/blocks/c01")
    assert detect_format(data, "设备.feishu-docx.json") == "feishu_docx"
    assert detect_format(data, "设备.json") == "json"  # Do not reinterpret arbitrary JSON files.


def test_references_unknown_blocks_and_unknown_inline_remain_partial_without_fetching():
    value = native_fixture(resources=True)
    paragraph = next(b for b in value["blocks"] if b["block_id"] == "paragraph")
    paragraph["text"]["elements"] = [
        {"text_run": {"content": "甲", "text_element_style": {"link": {"url": "javascript:bad()"}}}},
        {"mention_doc": {"token": "private-doc", "title": "只保留引用"}},
        {"text_run": {"content": "乙"}},
    ]
    data, ir = parsed(value)
    assert not ir.complete and ir.quality == "needs_review" and len(ir.warnings) == 3
    assert ir.elements[2].text == "甲\ufffc乙"
    assert ir.elements[2].metadata["inline"][1]["char_start"] == 1
    image = next(e for e in ir.elements if e.kind == "image")
    assert image.metadata["resource_status"] == "not_fetched"
    assert image.metadata["resource_reference"]["token"] == "image-private"
    assert json.loads(data)["blocks"][0]["future_type"] == {"new_field": "不可丢弃"}


@pytest.mark.parametrize(
    "change", ["missing", "parent", "duplicate", "orphan", "cycle", "second_root", "bool_revision"]
)
def test_snapshot_rejects_partial_or_ambiguous_trees(change):
    value = native_fixture()
    root = next(b for b in value["blocks"] if b["block_id"] == "root")
    if change == "missing":
        value["blocks"] = [b for b in value["blocks"] if b["block_id"] != "paragraph"]
    elif change == "parent":
        value["blocks"][0]["parent_id"] = "elsewhere"
    elif change == "duplicate":
        value["blocks"].append(copy.deepcopy(value["blocks"][0]))
    elif change == "orphan":
        root["children"].remove("after")
    elif change == "cycle":
        value["blocks"][0]["children"] = ["root"]
    elif change == "second_root":
        value["blocks"][0]["block_type"] = 1
    else:
        value["revision_id"] = True
    with pytest.raises(DocumentError, match="快照|块树|块"):
        parsed(value)


@pytest.mark.parametrize("change", ["overlap", "out_of_bounds", "covered_text", "dimensions", "spans"])
def test_uncertain_table_layout_preserves_all_text_without_inventing_spans(change):
    value = native_fixture()
    table = next(b for b in value["blocks"] if b["block_id"] == "table")
    prop = table["table"]["property"]
    if change == "overlap":
        prop["merge_info"][0] = {"row_span": 2, "col_span": 2}
    elif change == "out_of_bounds":
        prop["merge_info"][0]["row_span"] = 3
    elif change == "covered_text":
        cell = next(b for b in value["blocks"] if b["block_id"] == "c10")
        cell["children"] = ["covered"]
        value["blocks"].append(text_block("covered", "c10", "仍须保留"))
    elif change == "dimensions":
        prop["row_size"] = 3
    else:
        prop["merge_info"] = [{}]
    _, ir = parsed(value)
    assert not ir.complete
    assert not next(e for e in ir.elements if e.kind == "table").cells
    assert "季度检查" in ir.text and "年度更换" in ir.text
    if change == "covered_text":
        assert "仍须保留" in ir.text


@pytest.mark.parametrize(
    "limits",
    [
        ParseLimits(max_bytes=30),
        ParseLimits(max_elements=2),
        ParseLimits(max_characters=5),
        ParseLimits(max_cells=2),
    ],
)
def test_native_parse_enforces_budgets(limits):
    with pytest.raises(DocumentError) as result:
        parsed(limits=limits)
    assert result.value.code == "resource_limit"


@pytest.mark.parametrize(
    "data",
    [
        b'{"snapshot_version":1,"snapshot_version":2}',
        b'{"number":NaN}',
        b'{"number":1e500}',
        b'{"number":"\xff"}',
    ],
)
def test_invalid_json_never_becomes_a_partial_success(data):
    with pytest.raises(DocumentError):
        load_snapshot(data)


def test_no_docling_fallback_or_unversioned_snapshot():
    with pytest.raises(DocumentError) as failure:
        parsed(engine="docling")
    assert failure.value.code == "parser_unavailable"
    value = native_fixture()
    value["snapshot_version"] = "feishu-docx/2"
    with pytest.raises(DocumentError):
        parsed(value)
