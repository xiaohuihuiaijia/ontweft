"""Decode a versioned Feishu block snapshot, without fetching linked resources.

The snapshot is data, never an employee authorization proof. Origin binding is
the import service's responsibility. Unknown blocks remain in the original
snapshot and make the interpretation partial instead of disappearing silently.
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import PurePosixPath

from ontology.documents.models import (
    DocumentElement,
    DocumentError,
    DocumentIR,
    EvidenceRef,
    ParseLimits,
    TableCell,
)

SNAPSHOT_VERSION = "feishu-docx/1"
PARSER_VERSION = "1.0"
SUFFIX = ".feishu-docx.json"
TEXT_FIELDS = {
    1: "page",
    2: "text",
    **{n + 2: f"heading{n}" for n in range(1, 10)},
    12: "bullet",
    13: "ordered",
    14: "code",
    15: "quote",
    17: "todo",
}
CONTAINERS = {19, 24, 25, 32, 33, 34, 44, 45, 47}
RESOURCE_FIELDS = {
    18: "bitable",
    23: "file",
    26: "iframe",
    27: "image",
    29: "mindnote",
    30: "sheet",
    43: "board",
    48: "link_preview",
    49: "source_synced",
    50: "reference_synced",
}


def invalid(message="飞书原生快照结构不完整或无效"):
    raise DocumentError("invalid_feishu_snapshot", message)


def _identity(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,512}", value):
        invalid()
    return value


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            invalid("飞书快照包含重复 JSON 键")
        result[key] = value
    return result


def snapshot_bytes(value, limits=None):
    limits = limits or ParseLimits()
    try:
        data = json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
        ).encode("utf-8")
    except (ValueError, TypeError, RecursionError, UnicodeError):
        invalid()
    if len(data) > limits.max_bytes:
        raise DocumentError("resource_limit", "飞书原生快照超过字节预算")
    return data


def load_snapshot(data, limits=None):
    limits = limits or ParseLimits()
    if not data or len(data) > limits.max_bytes:
        raise DocumentError("resource_limit", "飞书原生快照为空或超过字节预算")
    try:
        value = json.loads(data, object_pairs_hook=_pairs, parse_constant=lambda _: invalid())
        # Also rejects exponent overflow to infinity and non-UTF-8 strings.
        snapshot_bytes(value, limits)
    except (ValueError, TypeError, RecursionError, UnicodeError):
        invalid()
    if not isinstance(value, dict) or value.get("snapshot_version") != SNAPSHOT_VERSION:
        invalid("无法识别飞书原生快照版本")
    _identity(value.get("document_id"))
    if type(value.get("revision_id")) is not int or not 0 <= value["revision_id"] < 2**63:
        invalid()
    if not isinstance(value.get("title"), str) or len(value["title"]) > limits.max_characters:
        invalid()
    blocks = value.get("blocks")
    if not isinstance(blocks, list) or not blocks:
        invalid()
    if len(blocks) > limits.max_elements:
        raise DocumentError("resource_limit", "飞书原生快照超过块数量预算")
    by_id, roots, children = {}, [], {}
    for block in blocks:
        if not isinstance(block, dict):
            invalid()
        key = _identity(block.get("block_id"))
        if key in by_id or type(block.get("block_type")) is not int:
            invalid("飞书原生快照包含重复块或无效类型")
        by_id[key] = block
        descendants = block.get("children", [])
        if not isinstance(descendants, list) or any(not isinstance(x, str) for x in descendants):
            invalid()
        if len(set(descendants)) != len(descendants):
            invalid("块树包含重复子块")
        children[key] = descendants
        if block["block_type"] == 1:
            roots.append(key)
    if len(roots) != 1 or by_id[roots[0]].get("parent_id") not in (None, ""):
        invalid("块树必须有且只有一个页面根节点")
    stack, order, visited = [(roots[0], 0)], [], set()
    while stack:
        key, depth = stack.pop()
        if key not in by_id or key in visited:
            invalid("块树含缺失子块、环或重复归属")
        if depth > 128:
            raise DocumentError("resource_limit", "飞书块树超过深度预算")
        visited.add(key)
        order.append(key)
        for child in children[key]:
            if child not in by_id or by_id[child].get("parent_id") != key:
                invalid("块树的父子关系不一致")
        stack.extend((child, depth + 1) for child in reversed(children[key]))
    if len(visited) != len(by_id):
        invalid("快照含不能从页面根节点访问的块")
    return value, by_id, children, order


def parse_native(data, filename, *, source_id, source_revision, limits=None):
    limits = limits or ParseLimits()
    value, blocks, children, order = load_snapshot(data, limits)
    ir = DocumentIR(
        source_id=source_id,
        source_revision=source_revision,
        filename=PurePosixPath(filename.replace("\\", "/")).name,
        content_hash=hashlib.sha256(data).hexdigest(),
        format="feishu_docx",
        parser="basic/feishu_docx",
        parser_version=PARSER_VERSION,
        quality="structured",
    )
    total_characters = total_cells = 0
    skipped = set()

    def warn(message):
        ir.complete, ir.quality = False, "needs_review"
        if message not in ir.warnings:
            ir.warnings.append(message)

    def path(key):
        return f"feishu/documents/{value['document_id']}/revisions/{value['revision_id']}/blocks/{key}"

    def ref(key, text=None):
        return EvidenceRef(
            source_id=source_id,
            source_revision=source_revision,
            content_hash=ir.content_hash,
            element_path=path(key),
            char_start=0 if text is not None else None,
            char_end=len(text) if text is not None else None,
        )

    def metadata(key):
        block = blocks[key]
        return {
            "block_id": key,
            "block_type": block["block_type"],
            "parent_id": block.get("parent_id"),
            "children": children[key],
            "remote_document_id": value["document_id"],
            "remote_revision_id": value["revision_id"],
            "original_snapshot_path": f"blocks/{key}",
        }

    def text_of(key):
        block = blocks[key]
        field = TEXT_FIELDS.get(block["block_type"])
        if field is None:
            return "", []
        content = block.get(field)
        if not isinstance(content, dict) or not isinstance(content.get("elements", []), list):
            invalid("文本块缺少合法内容")
        parts, inline, position = [], [], 0
        for index, element in enumerate(content.get("elements", [])):
            if not isinstance(element, dict) or len(element) != 1:
                invalid("文本行内元素结构不明确")
            kind, payload = next(iter(element.items()))
            if not isinstance(payload, dict):
                invalid()
            if kind in {"text_run", "equation"}:
                text = payload.get("content")
            else:
                # Object replacement prevents adjacent text runs being silently
                # joined across a resource/unknown inline element.
                text = "\ufffc"
                warn("包含未展开的行内引用；原始字段保留在快照中，未读取其目标资源。")
            if not isinstance(text, str):
                invalid("文本行内元素缺少文本")
            inline.append(
                {
                    "index": index,
                    "kind": kind,
                    "char_start": position,
                    "char_end": position + len(text),
                    "data": payload,
                }
            )
            position += len(text)
            parts.append(text)
        return "".join(parts), inline

    def add(key, kind, text="", **kw):
        nonlocal total_characters, total_cells
        cells = kw.get("cells", [])
        total_characters += max(len(text), sum(len(cell.text) for cell in cells))
        total_cells += len(cells)
        if (
            len(ir.elements) >= limits.max_elements
            or total_characters > limits.max_characters
            or total_cells > limits.max_cells
        ):
            raise DocumentError("resource_limit", "飞书文档解析超过结构或文字预算")
        ir.elements.append(
            DocumentElement(
                element_id=hashlib.sha256((ir.content_hash + ":" + path(key)).encode()).hexdigest(),
                kind=kind,
                order=len(ir.elements),
                text=text,
                evidence=ref(key, text) if text else ref(key),
                **kw,
            )
        )

    def descendants(key):
        result, pending = [], list(reversed(children[key]))
        while pending:
            child = pending.pop()
            result.append(child)
            pending.extend(reversed(children[child]))
        return result

    def table(key):
        table = blocks[key].get("table")
        if not isinstance(table, dict) or not isinstance(table.get("property"), dict):
            return False
        props, ids = table["property"], table.get("cells")
        rows, cols = props.get("row_size"), props.get("column_size")
        if type(rows) is not int or type(cols) is not int or rows <= 0 or cols <= 0:
            return False
        if rows * cols > limits.max_cells:
            raise DocumentError("resource_limit", "飞书表格超过单元格预算")
        if (
            not isinstance(ids, list)
            or len(ids) != rows * cols
            or ids != children[key]
            or any(blocks[c]["block_type"] != 32 for c in ids)
        ):
            return False
        spans = props.get("merge_info") or [{"row_span": 1, "col_span": 1} for _ in ids]
        if not isinstance(spans, list) or len(spans) != len(ids):
            return False
        coverage, cells, cell_blocks = set(), [], {}
        for index, cell_id in enumerate(ids):
            row, column = divmod(index, cols)
            content_ids = descendants(cell_id)
            if any(
                blocks[c]["block_type"] not in TEXT_FIELDS and blocks[c]["block_type"] not in CONTAINERS
                for c in content_ids
            ):
                return False
            fragments = [(c, *text_of(c)) for c in content_ids if blocks[c]["block_type"] in TEXT_FIELDS]
            text = "\n".join(fragment[1] for fragment in fragments)
            span = spans[index]
            if not isinstance(span, dict):
                return False
            rs, cs = span.get("row_span"), span.get("col_span")
            if type(rs) is not int or type(cs) is not int:
                return False
            if (row, column) in coverage:
                if (rs, cs) != (0, 0) or text:
                    return False
                continue
            if not (1 <= rs <= rows - row and 1 <= cs <= cols - column):
                return False
            positions = {(r, c) for r in range(row, row + rs) for c in range(column, column + cs)}
            if positions & coverage:
                return False
            coverage.update(positions)
            cell_blocks[cell_id] = [{"block_id": c, "inline": runs} for c, _, runs in fragments]
            cells.append(
                TableCell(
                    row=row,
                    column=column,
                    row_span=rs,
                    column_span=cs,
                    text=text,
                    evidence=ref(cell_id, text),
                    column_header=props.get("header_row") is True and row == 0,
                    row_header=props.get("header_column") is True and column == 0,
                )
            )
        add(
            key,
            "table",
            cells=cells,
            rows=rows,
            columns=cols,
            metadata={**metadata(key), "cell_blocks": cell_blocks, "table_property": props},
        )
        skipped.update(descendants(key))
        return True

    for key in order:
        if key in skipped:
            continue
        block = blocks[key]
        kind = block["block_type"]
        meta = metadata(key)
        if kind in TEXT_FIELDS:
            text, inline = text_of(key)
            meta.update(inline=inline, text_style=block[TEXT_FIELDS[kind]].get("style", {}))
            element_kind = (
                "heading"
                if kind == 1 or 3 <= kind <= 11
                else "list_item"
                if kind in {12, 13, 17}
                else "code"
                if kind == 14
                else "paragraph"
            )
            add(
                key,
                element_kind,
                text,
                level=1 if kind == 1 else kind - 2 if 3 <= kind <= 11 else None,
                metadata=meta,
            )
        elif kind == 31:
            if not table(key):
                warn("表格结构或合并关系无法确认；保留各子块文字及原始快照，未推测单元格跨度。")
                add(key, "table", metadata={**meta, "layout_unresolved": True})
        elif kind in RESOURCE_FIELDS:
            field = RESOURCE_FIELDS[kind]
            warn("包含图片、附件或其他关联资源；当前仅保留引用，未下载、OCR 或继承其权限。")
            add(
                key,
                "image" if kind == 27 else "paragraph",
                metadata={
                    **meta,
                    "resource_kind": field,
                    "resource_reference": block.get(field),
                    "resource_status": "not_fetched",
                },
            )
        elif kind in CONTAINERS or kind == 22:
            # Structural parents remain available in every child's path and in
            # the original snapshot; visual-only dividers contain no source text.
            continue
        else:
            warn("包含尚未解释的块类型；原始块已保留在快照中。")
            add(key, "paragraph", metadata={**meta, "unsupported_block": True})
    return ir
