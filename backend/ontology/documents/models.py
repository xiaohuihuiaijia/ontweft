"""Version-bound document structure; rendering coordinates are never invented."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


class DocumentError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


class ParseLimits(BaseModel):
    max_bytes: int = Field(default=256 * 1024 * 1024, ge=1)
    max_expanded_bytes: int = Field(default=200 * 1024 * 1024, ge=1)
    max_archive_entries: int = Field(default=10_000, ge=1)
    max_compression_ratio: int = Field(default=1000, ge=1)
    max_pages: int = Field(default=1000, ge=1)
    max_elements: int = Field(default=100_000, ge=1)
    max_characters: int = Field(default=10_000_000, ge=1)
    max_cells: int = Field(default=500_000, ge=1)
    max_images: int = Field(default=500, ge=0)


class EvidenceRef(BaseModel):
    source_id: str
    source_revision: str
    content_hash: str
    element_path: str
    page_number: int | None = None  # 1-based, PDF only unless a render version is provided.
    bbox: tuple[float, float, float, float] | None = None
    coordinate_system: str | None = None
    sheet: str | None = None
    cell_address: str | None = None
    line_start: int | None = None
    line_end: int | None = None
    char_start: int | None = None
    char_end: int | None = None


class TableCell(BaseModel):
    row: int  # 0-based
    column: int
    row_span: int = 1
    column_span: int = 1
    text: str = ""
    value: Any = None
    data_type: str | None = None
    formula: str | None = None
    cached_value: Any = None
    evidence: EvidenceRef
    column_header: bool = False
    row_header: bool = False


class DocumentElement(BaseModel):
    element_id: str
    kind: Literal["heading", "paragraph", "list_item", "table", "image", "code", "formula"]
    order: int
    text: str = ""
    level: int | None = None
    evidence: EvidenceRef
    provenance: list[EvidenceRef] = Field(default_factory=list)
    cells: list[TableCell] = Field(default_factory=list)
    rows: int | None = None
    columns: int | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class PageStatus(BaseModel):
    page_number: int
    status: Literal["parsed", "needs_ocr", "failed", "skipped"]
    width: float | None = None
    height: float | None = None
    rotation: int = 0
    reason: str | None = None


class DocumentIR(BaseModel):
    ir_version: str = "1.0"
    source_id: str
    source_revision: str
    filename: str
    content_hash: str
    format: str
    parser: str
    parser_version: str
    quality: Literal["basic", "structured", "needs_review"] = "basic"
    complete: bool = True
    elements: list[DocumentElement] = Field(default_factory=list)
    source_elements: list[DocumentElement] = Field(default_factory=list)
    pages: list[PageStatus] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)

    @property
    def text(self) -> str:
        return "\n\n".join(element.text for element in self.elements if element.text)
