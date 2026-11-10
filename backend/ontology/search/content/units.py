"""Extract version-bound text units without inventing source coordinates.

Authorization, published-revision selection and index freshness are caller
responsibilities. This module checks IR identity, budgets and exact text ranges.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Iterable
from copy import deepcopy
from typing import Any

from pydantic import ValidationError

from ontology.documents.models import DocumentError, DocumentIR, ParseLimits

CHUNKER_VERSION = "content-units-v1-2048-overlap256"
CHUNK_CHARACTERS = 2048
CHUNK_OVERLAP = 256
LIMITS = ParseLimits()
_DOCUMENT_KEYS = {
    "layer",
    "element_index",
    "element_id",
    "cell_index",
    "unit_kind",
    "unit_text_start",
    "unit_text_end",
}
_KNOWLEDGE_KEYS = {"field", "unit_text_start", "unit_text_end"}


def _invalid_ir() -> DocumentError:
    return DocumentError("invalid_document_ir", "Document structure is not valid version-bound JSON")


def _json_bytes(value: dict) -> bytes:
    if not isinstance(value, dict):
        raise _invalid_ir()
    pending, seen = [value], set()
    while pending:
        item = pending.pop()
        if type(item) in {dict, list}:
            if id(item) in seen:
                continue
            seen.add(id(item))
            if isinstance(item, dict):
                if any(not isinstance(key, str) for key in item):
                    raise _invalid_ir()
                pending.extend(item.values())
            else:
                pending.extend(item)
        elif type(item) not in {str, int, float, bool, type(None)} or (
            isinstance(item, float) and not math.isfinite(item)
        ):
            raise _invalid_ir()
    try:
        return json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
        ).encode("utf-8")
    except (TypeError, ValueError, RecursionError, UnicodeError):
        raise _invalid_ir() from None


def ir_fingerprint(ir_dict: dict) -> str:
    """Hash the complete original JSON, including metadata and parser details."""
    return hashlib.sha256(_json_bytes(ir_dict)).hexdigest()


def _check_budget(raw: dict) -> None:
    elements = cells = characters = 0
    for layer in ("elements", "source_elements"):
        values = raw.get(layer, [])
        if not isinstance(values, list):
            raise _invalid_ir()
        elements += len(values)
        if elements > LIMITS.max_elements:
            raise DocumentError("resource_limit", "Document exceeds the element budget")
        for item in values:
            if not isinstance(item, dict) or not isinstance(item.get("text", ""), str):
                raise _invalid_ir()
            table_cells = item.get("cells", [])
            if not isinstance(table_cells, list):
                raise _invalid_ir()
            cells += len(table_cells)
            if cells > LIMITS.max_cells:
                raise DocumentError("resource_limit", "Document exceeds the cell budget")
            cell_characters = 0
            for cell in table_cells:
                if not isinstance(cell, dict) or not isinstance(cell.get("text", ""), str):
                    raise _invalid_ir()
                cell_characters += len(cell.get("text", ""))
            # Table.text ordinarily repeats its cells. Do not count that same
            # logical text twice, but a missing summary cannot hide large cells.
            characters += max(len(item.get("text", "")), cell_characters)
            if characters > LIMITS.max_characters:
                raise DocumentError("resource_limit", "Document exceeds the character budget")


def validate_document_ir(document: Any) -> DocumentIR:
    raw = document.ir
    if not isinstance(raw, dict):
        raise _invalid_ir()
    _check_budget(raw)
    # Also reject NaN, non-JSON metadata and invalid string encoding instead of
    # losing them in a coerced Pydantic dump or a partial fingerprint.
    _json_bytes(raw)
    try:
        ir = DocumentIR.model_validate(raw)
    except (ValidationError, ValueError, TypeError, RecursionError):
        raise _invalid_ir() from None
    expected = (document.id, str(document.revision), document.content_hash)

    def bound(reference: Any) -> None:
        if (reference.source_id, reference.source_revision, reference.content_hash) != expected:
            raise DocumentError(
                "invalid_evidence", "Document evidence differs from the immutable source identity"
            )

    bound(ir)
    for element in [*ir.elements, *ir.source_elements]:
        bound(element.evidence)
        for reference in element.provenance:
            bound(reference)
        for cell in element.cells:
            bound(cell.evidence)
    return ir


def _layers(ir: DocumentIR) -> tuple[str, ...]:
    return (
        ("source_elements",)
        if ir.format == "xlsx" and ir.source_elements
        else ("elements", "source_elements")
    )


def _ranges(text: str) -> Iterable[tuple[int, int]]:
    start = 0
    while start < len(text):
        end = min(start + CHUNK_CHARACTERS, len(text))
        if text[start:end].strip():
            yield start, end
        if end == len(text):
            return
        start = end - CHUNK_OVERLAP


def document_chunks(document: Any) -> Iterable[dict]:
    ir = validate_document_ir(document)
    for layer in _layers(ir):
        for element_index, element in enumerate(getattr(ir, layer)):
            units = (
                ((index, "table_cell", cell.text) for index, cell in enumerate(element.cells))
                if element.kind == "table" and element.cells
                else ((None, element.kind, element.text),)
            )
            for cell_index, kind, text in units:
                for start, end in _ranges(text):
                    yield {
                        "text": text[start:end],
                        "anchor": {
                            "layer": layer,
                            "element_index": element_index,
                            "element_id": element.element_id,
                            "cell_index": cell_index,
                            "unit_kind": kind,
                            "unit_text_start": start,
                            "unit_text_end": end,
                        },
                    }


def _knowledge_texts(revision: Any) -> tuple[tuple[str, str], ...]:
    pairs = (("title", revision.title), ("body", revision.body))
    if any(not isinstance(text, str) for _, text in pairs):
        raise DocumentError("invalid_knowledge", "Knowledge revision text is invalid")
    if sum(len(text) for _, text in pairs) > LIMITS.max_characters:
        raise DocumentError("resource_limit", "Knowledge revision exceeds the character budget")
    return pairs


def knowledge_chunks(revision: Any) -> Iterable[dict]:
    for field, text in _knowledge_texts(revision):
        for start, end in _ranges(text):
            yield {
                "text": text[start:end],
                "anchor": {
                    "field": field,
                    "unit_text_start": start,
                    "unit_text_end": end,
                },
            }


def _invalid_anchor() -> DocumentError:
    return DocumentError("invalid_anchor", "Anchor does not identify an exact source text range")


def _slice(text: str, anchor: dict) -> str:
    start, end = anchor["unit_text_start"], anchor["unit_text_end"]
    if type(start) is not int or type(end) is not int or not 0 <= start < end <= len(text):
        raise _invalid_anchor()
    return text[start:end]


def resolve_document_anchor(document: Any, anchor: dict) -> dict:
    ir = validate_document_ir(document)
    if not isinstance(anchor, dict) or set(anchor) != _DOCUMENT_KEYS:
        raise _invalid_anchor()
    layer, index, cell_index = anchor["layer"], anchor["element_index"], anchor["cell_index"]
    if not isinstance(layer, str) or layer not in _layers(ir) or type(index) is not int:
        raise _invalid_anchor()
    elements = getattr(ir, layer)
    if not 0 <= index < len(elements):
        raise _invalid_anchor()
    element = elements[index]
    if anchor["element_id"] != element.element_id:
        raise _invalid_anchor()
    metadata = deepcopy(element.metadata)
    if element.kind == "table" and element.cells:
        if (
            type(cell_index) is not int
            or not 0 <= cell_index < len(element.cells)
            or anchor["unit_kind"] != "table_cell"
        ):
            raise _invalid_anchor()
        cell = element.cells[cell_index]
        text, evidence = cell.text, cell.evidence
        metadata["table_cell"] = cell.model_dump(mode="json", exclude={"text", "evidence"})
        # Element/table provenance remains explicitly contextual; it never
        # supplies a guessed cell page/bbox when the cell has none.
        metadata["provenance_scope"] = "parent_table"
    else:
        if cell_index is not None or anchor["unit_kind"] != element.kind:
            raise _invalid_anchor()
        text, evidence = element.text, element.evidence
    return {
        "text": text,
        "chunk_text": _slice(text, anchor),
        "evidence": evidence.model_dump(mode="json"),
        "provenance": [ref.model_dump(mode="json") for ref in element.provenance],
        "metadata": metadata,
    }


def resolve_knowledge_anchor(revision: Any, anchor: dict) -> dict:
    pairs = dict(_knowledge_texts(revision))
    if not isinstance(anchor, dict) or set(anchor) != _KNOWLEDGE_KEYS:
        raise _invalid_anchor()
    field = anchor["field"]
    if not isinstance(field, str) or field not in pairs:
        raise _invalid_anchor()
    text = pairs[field]
    return {
        "text": text,
        "chunk_text": _slice(text, anchor),
        "evidence": None,
        "provenance": [],
        "metadata": {"field": field},
    }
