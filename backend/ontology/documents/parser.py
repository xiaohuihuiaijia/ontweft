"""Small audited format paths. Basic extraction is not layout/OCR equivalence.

Run parsing in a resource-limited worker. Byte/structure checks here supplement,
not replace, process memory/CPU/time limits on adversarial document decoders.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import posixpath
import re
import zipfile
from importlib.metadata import version
from pathlib import Path, PurePosixPath
from typing import Any

from ontology.documents.models import (
    DocumentElement,
    DocumentError,
    DocumentIR,
    EvidenceRef,
    PageStatus,
    ParseLimits,
    TableCell,
)

W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
R = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"
A = "{http://schemas.openxmlformats.org/drawingml/2006/main}"


def _zip(data: bytes, limits: ParseLimits) -> zipfile.ZipFile:
    return _archive(io.BytesIO(data), limits)


def _archive(source, limits: ParseLimits) -> zipfile.ZipFile:
    try:
        archive = zipfile.ZipFile(source)
        infos = archive.infolist()
        if len(infos) > limits.max_archive_entries:
            raise DocumentError("resource_limit", "Archive has too many members")
        total, seen = 0, set()
        for item in infos:
            if item.filename in seen:
                raise DocumentError("invalid_archive", "Duplicate archive member")
            seen.add(item.filename)
            path = PurePosixPath(item.filename.replace("\\", "/"))
            if path.is_absolute() or ".." in path.parts:
                raise DocumentError("invalid_archive", "Unsafe archive member path")
            if item.flag_bits & 1:
                raise DocumentError("encrypted", "Encrypted document archive is not supported")
            total += item.file_size
            if (
                total > limits.max_expanded_bytes
                or item.file_size > max(item.compress_size, 1) * limits.max_compression_ratio
            ):
                raise DocumentError("resource_limit", "Expanded archive exceeds its budget")
        return archive
    except DocumentError:
        raise
    except (zipfile.BadZipFile, OSError, ValueError):
        raise DocumentError("invalid_archive", "Document archive is corrupt") from None


def detect_format(data: bytes, filename: str, limits: ParseLimits | None = None) -> str:
    limits = limits or ParseLimits()
    if not data:
        raise DocumentError("empty_document", "Document is empty")
    if len(data) > limits.max_bytes:
        raise DocumentError("resource_limit", "Document exceeds the byte limit")
    from ontology.documents.feishu_native import SUFFIX, load_snapshot

    if filename.lower().endswith(SUFFIX):
        load_snapshot(data, limits)
        return "feishu_docx"
    suffix = PurePosixPath(filename.lower().replace("\\", "/")).suffix
    detected = None
    if data.startswith(b"%PDF-"):
        detected = "pdf"
    elif data.startswith(b"\x89PNG\r\n\x1a\n"):
        detected = "png"
    elif data.startswith(b"\xff\xd8\xff"):
        detected = "jpeg"
    elif data.startswith(b"PK\x03\x04"):
        with _zip(data, limits) as archive:
            names = set(archive.namelist())
            if "word/document.xml" in names:
                detected = "docx"
            elif "xl/workbook.xml" in names:
                detected = "xlsx"
            elif "ppt/presentation.xml" in names:
                detected = "pptx"
            else:
                raise DocumentError("unsupported_format", "Archive is not supported OOXML")
    elif data.startswith(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"):
        raise DocumentError("conversion_required", "Legacy Office requires the isolated conversion service")
    if detected:
        expected = {".jpg", ".jpeg"} if detected == "jpeg" else {"." + detected}
        if suffix and suffix not in expected:
            raise DocumentError("format_mismatch", "Filename extension does not match document content")
        return detected
    if suffix in {".pdf", ".docx", ".xlsx", ".pptx", ".png", ".jpg", ".jpeg"}:
        raise DocumentError("format_mismatch", "Document signature does not match filename")
    return {".txt": "txt", ".md": "markdown", ".markdown": "markdown", ".csv": "csv", ".json": "json"}.get(
        suffix
    ) or _unsupported()


def _unsupported() -> str:
    raise DocumentError("unsupported_format", "This format has no enabled parser")


def detect_format_path(path: Path, filename: str, limits: ParseLimits | None = None) -> str:
    """Inspect uploaded signatures without loading a large file into RAM."""
    limits = limits or ParseLimits()
    size = path.stat().st_size
    if not size or size > limits.max_bytes:
        raise DocumentError("resource_limit", "Document is empty or exceeds the byte limit")
    with path.open("rb") as handle:
        header = handle.read(16)
    if header.startswith(b"PK\x03\x04"):
        with _archive(path, limits) as archive:
            names = set(archive.namelist())
            formats = {"word/document.xml": "docx", "xl/workbook.xml": "xlsx", "ppt/presentation.xml": "pptx"}
            detected = next((fmt for member, fmt in formats.items() if member in names), None)
            if not detected:
                return _unsupported()
            if Path(filename).suffix.lower() != "." + detected:
                raise DocumentError("format_mismatch", "Filename extension does not match document content")
            return detected
    from ontology.documents.feishu_native import SUFFIX

    if filename.lower().endswith(SUFFIX):
        return detect_format(path.read_bytes(), filename, limits)
    return detect_format(header, filename, limits)


def _decode(data: bytes) -> str:
    try:
        text = data.decode("utf-16" if data.startswith((b"\xff\xfe", b"\xfe\xff")) else "utf-8-sig")
    except UnicodeError:
        raise DocumentError("unsupported_encoding", "Text must be UTF-8 or BOM-marked UTF-16") from None
    if "\x00" in text:
        raise DocumentError("invalid_text", "Text contains NUL bytes")
    return text


class _Builder:
    def __init__(self, ir: DocumentIR, limits: ParseLimits):
        self.ir, self.limits = ir, limits
        self.characters = self.cells = 0

    def ref(self, path: str, **kwargs: Any) -> EvidenceRef:
        return EvidenceRef(
            source_id=self.ir.source_id,
            source_revision=self.ir.source_revision,
            content_hash=self.ir.content_hash,
            element_path=path,
            **kwargs,
        )

    def add(self, kind: str, text: str, path: str, **kwargs: Any) -> DocumentElement:
        self.characters += len(text)
        self.cells += len(kwargs.get("cells", []))
        if (
            len(self.ir.elements) >= self.limits.max_elements
            or self.characters > self.limits.max_characters
            or self.cells > self.limits.max_cells
        ):
            raise DocumentError("resource_limit", "Document structure exceeds its budget")
        evidence = kwargs.pop("evidence", None) or self.ref(path)
        item = DocumentElement(
            element_id=hashlib.sha256((self.ir.content_hash + ":" + path).encode()).hexdigest(),
            kind=kind,
            order=len(self.ir.elements),
            text=text,
            evidence=evidence,
            **kwargs,
        )
        self.ir.elements.append(item)
        return item


def _xml(data: bytes) -> Any:
    from defusedxml.ElementTree import fromstring

    try:
        return fromstring(data, forbid_dtd=True, forbid_entities=True, forbid_external=True)
    except Exception:  # noqa: BLE001 -- decoder failures are classified without exposing source data.
        raise DocumentError("invalid_xml", "Document XML is invalid or contains forbidden entities") from None


def _word_text(element: Any) -> str:
    pieces = []
    for node in element.iter():
        if node.tag == W + "t":
            pieces.append(node.text or "")
        elif node.tag == W + "tab":
            pieces.append("\t")
        elif node.tag in {W + "br", W + "cr"}:
            pieces.append("\n")
    return "".join(pieces)


def _docx(data: bytes, b: _Builder) -> None:
    with _zip(data, b.limits) as archive:
        root = _xml(archive.read("word/document.xml"))
        body = root.find(W + "body")
        if body is None:
            raise DocumentError("invalid_document", "Word document has no supported body")
        rels = {}
        if "word/_rels/document.xml.rels" in archive.namelist():
            for rel in _xml(archive.read("word/_rels/document.xml.rels")):
                if rel.get("TargetMode") != "External":
                    rels[rel.get("Id")] = rel.get("Target", "")
        image_count = 0
        for index, child in enumerate(body):
            path = f"word/document.xml/body/{index}"
            if child.tag == W + "p":
                text = _word_text(child)
                style = child.find(W + "pPr/" + W + "pStyle")
                style_name = style.get(W + "val", "") if style is not None else ""
                heading = re.fullmatch(r"(?:Heading|heading|标题)([1-9])", style_name)
                kind = (
                    "heading"
                    if heading
                    else "list_item"
                    if child.find(W + "pPr/" + W + "numPr") is not None
                    else "paragraph"
                )
                b.add(
                    kind,
                    text,
                    path,
                    level=int(heading.group(1)) if heading else None,
                    evidence=b.ref(path, char_start=0, char_end=len(text)),
                )
            elif child.tag == W + "tbl":
                cells, active = [], {}
                rows = child.findall(W + "tr")
                width = 0
                for ri, row in enumerate(rows):
                    before = row.find(W + "trPr/" + W + "gridBefore")
                    col = int(before.get(W + "val", "0")) if before is not None else 0
                    new_active = {}
                    for ci, cell in enumerate(row.findall(W + "tc")):
                        span_node = cell.find(W + "tcPr/" + W + "gridSpan")
                        span = max(1, int(span_node.get(W + "val", "1"))) if span_node is not None else 1
                        merge = cell.find(W + "tcPr/" + W + "vMerge")
                        continuing = merge is not None and merge.get(W + "val", "continue") == "continue"
                        text = "\n".join(_word_text(p) for p in cell.findall(W + "p"))
                        if cell.find(W + "tbl") is not None:
                            b.ir.warnings.append("Nested Word table requires structured parser review")
                            text = _word_text(cell)
                        if continuing and col in active and active[col].column_span == span:
                            target = active[col]
                            target.row_span += 1
                            if text:
                                target.text += "\n" + text
                            new_active[col] = target
                        else:
                            if continuing:
                                b.ir.warnings.append(
                                    "Unresolved vertical merge retained as an independent cell"
                                )
                            target = TableCell(
                                row=ri,
                                column=col,
                                column_span=span,
                                text=text,
                                evidence=b.ref(f"{path}/row/{ri}/cell/{ci}"),
                            )
                            cells.append(target)
                            if merge is not None:
                                new_active[col] = target
                        col += span
                    active = new_active
                    width = max(width, col)
                b.add(
                    "table",
                    "\n".join(c.text for c in cells),
                    path,
                    cells=cells,
                    rows=len(rows),
                    columns=width,
                )
            elif child.tag != W + "sectPr":
                b.ir.complete, b.ir.quality = False, "needs_review"
                b.ir.warnings.append(
                    f"Unsupported Word body container at {path}; structured reprocessing required"
                )
            for image_index, blip in enumerate(child.iter(A + "blip")):
                if image_count >= b.limits.max_images:
                    b.ir.complete, b.ir.quality = False, "needs_review"
                    if "Image budget reached; subsequent text was still processed" not in b.ir.warnings:
                        b.ir.warnings.append("Image budget reached; subsequent text was still processed")
                    continue
                target = rels.get(blip.get(R + "embed"))
                if not target:
                    continue
                member = posixpath.normpath(posixpath.join("word", target))
                if not member.startswith("word/") or member not in archive.namelist():
                    b.ir.complete, b.ir.quality = False, "needs_review"
                    b.ir.warnings.append("Unresolved embedded image")
                    continue
                blob = archive.read(member)
                b.add(
                    "image",
                    "",
                    f"{path}/image/{image_index}",
                    metadata={
                        "content_hash": hashlib.sha256(blob).hexdigest(),
                        "archive_member": member,
                        "bytes": len(blob),
                    },
                )
                image_count += 1
                b.ir.complete, b.ir.quality = False, "needs_review"
                if "Embedded images require visual understanding" not in b.ir.warnings:
                    b.ir.warnings.append("Embedded images require visual understanding")
        for member in archive.namelist():
            if member in {"word/footnotes.xml", "word/endnotes.xml"} or re.fullmatch(
                r"word/(?:header|footer)\d+\.xml", member
            ):
                part = _xml(archive.read(member))
                for index, paragraph in enumerate(part.iter(W + "p")):
                    text = _word_text(paragraph)
                    if text:
                        b.add(
                            "paragraph",
                            text,
                            f"{member}/paragraph/{index}",
                            metadata={"context_part": member},
                        )
        b.ir.warnings.append("DOCX anchors are structural; no physical page numbers are inferred")
        b.ir.warnings.append(
            "Basic Word path preserves note/header/footer text; tracked-change semantics and complex nested layout require review"
        )


def _pdf(data: bytes, b: _Builder) -> None:
    from ontology.documents.pdf_native import extract_pages

    extract_pages(data, b)


def _xlsx(data: bytes, b: _Builder) -> None:
    from openpyxl import load_workbook

    with _zip(data, b.limits) as archive:
        # Harden XML before openpyxl; macros/external references are never executed.
        for member in archive.namelist():
            if member.endswith((".xml", ".rels")):
                _xml(archive.read(member))
    formulas = values = None
    try:
        formulas = load_workbook(io.BytesIO(data), data_only=False, keep_links=False)
        values = load_workbook(io.BytesIO(data), data_only=True, keep_links=False)
        for sheet in formulas.worksheets:
            if sheet.max_row * sheet.max_column > b.limits.max_cells:
                raise DocumentError("resource_limit", "Worksheet exceeds its cell budget")
            merged = {}
            for area in sheet.merged_cells.ranges:
                merged[(area.min_row, area.min_col)] = (
                    area.max_row - area.min_row + 1,
                    area.max_col - area.min_col + 1,
                )
            cells = []
            for row in sheet.iter_rows():
                for cell in row:
                    if cell.value is None and (cell.row, cell.column) not in merged:
                        continue
                    row_span, column_span = merged.get((cell.row, cell.column), (1, 1))
                    formula = cell.value if cell.data_type == "f" else None
                    cached = values[sheet.title][cell.coordinate].value if formula else None
                    cells.append(
                        TableCell(
                            row=cell.row - 1,
                            column=cell.column - 1,
                            row_span=row_span,
                            column_span=column_span,
                            text=str(cell.value),
                            value=None if formula else cell.value,
                            data_type=cell.data_type,
                            formula=formula,
                            cached_value=cached,
                            evidence=b.ref(
                                f"xlsx/{sheet.title}/{cell.coordinate}",
                                sheet=sheet.title,
                                cell_address=cell.coordinate,
                            ),
                        )
                    )
            b.add(
                "table",
                "\n".join(c.text for c in cells),
                f"xlsx/{sheet.title}",
                cells=cells,
                rows=sheet.max_row,
                columns=sheet.max_column,
                metadata={"sheet": sheet.title},
            )
        b.ir.warnings.append("Formulas are preserved, never evaluated; cached values may be missing or stale")
        b.ir.parser_version = version("openpyxl")
        _xlsx_images(data, b)
    finally:
        if formulas:
            formulas.close()
        if values:
            values.close()


def _xlsx_images(data: bytes, b: _Builder) -> None:
    from ontology.documents.slides import _relationships

    ns = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
    with _zip(data, b.limits) as archive:
        workbook = _xml(archive.read("xl/workbook.xml"))
        sheets = _relationships(archive, "xl/workbook.xml")
        count = 0
        for sheet in workbook.findall(ns + "sheets/" + ns + "sheet"):
            member = sheets.get(sheet.get(R + "id"))
            if not member or member not in archive.namelist():
                continue
            root = _xml(archive.read(member))
            rels = _relationships(archive, member)
            for drawing in root.findall(ns + "drawing"):
                drawing_member = rels.get(drawing.get(R + "id"))
                if not drawing_member or drawing_member not in archive.namelist():
                    continue
                picture_rels = _relationships(archive, drawing_member)
                for index, blip in enumerate(_xml(archive.read(drawing_member)).iter(A + "blip")):
                    target = picture_rels.get(blip.get(R + "embed"))
                    if not target or not target.startswith("xl/") or target not in archive.namelist():
                        continue
                    count += 1
                    if count > b.limits.max_images:
                        b.ir.complete, b.ir.quality = False, "needs_review"
                        b.ir.warnings.append("Workbook image budget reached")
                        return
                    blob = archive.read(target)
                    path = f"{drawing_member}/image/{index}"
                    b.add(
                        "image",
                        "",
                        path,
                        evidence=b.ref(path, sheet=sheet.get("name")),
                        metadata={
                            "sheet": sheet.get("name"),
                            "archive_member": target,
                            "content_hash": hashlib.sha256(blob).hexdigest(),
                            "bytes": len(blob),
                        },
                    )
                    b.ir.complete, b.ir.quality = False, "needs_review"


def _text(data: bytes, fmt: str, b: _Builder) -> None:
    text = _decode(data)
    if len(text) > b.limits.max_characters:
        raise DocumentError("resource_limit", "Text exceeds its character budget")
    if fmt == "json":
        from decimal import Decimal

        def unique_pairs(items: list[tuple[str, Any]]) -> dict:
            output = {}
            for key, value in items:
                if key in output:
                    raise DocumentError("invalid_json", "Duplicate JSON object key")
                output[key] = value
            return output

        def invalid_constant(value: str) -> None:
            raise DocumentError("invalid_json", "Non-finite JSON numeric literal")

        try:
            value = json.loads(
                text, parse_float=Decimal, parse_constant=invalid_constant, object_pairs_hook=unique_pairs
            )
        except DocumentError:
            raise
        except (ValueError, RecursionError):
            raise DocumentError("invalid_json", "JSON syntax or nesting is invalid") from None
        # Keep raw text and typed root; do not silently flatten arrays or lose absent/null distinctions.
        b.add("code", text, "json/root", metadata={"value": value, "json_pointer": ""})
    elif fmt == "csv":
        cells, row_count, width = [], 0, 0
        try:
            for ri, row in enumerate(csv.reader(io.StringIO(text, newline=""), strict=True)):
                if len(cells) + len(row) > b.limits.max_cells:
                    raise DocumentError("resource_limit", "CSV exceeds its cell budget")
                for ci, value in enumerate(row):
                    cells.append(
                        TableCell(
                            row=ri,
                            column=ci,
                            text=value,
                            value=value,
                            data_type="string",
                            evidence=b.ref(f"csv/row/{ri}/cell/{ci}"),
                        )
                    )
                row_count, width = ri + 1, max(width, len(row))
        except csv.Error:
            raise DocumentError("invalid_csv", "CSV syntax or field size is invalid") from None
        b.add("table", text, "csv/table", cells=cells, rows=row_count, columns=width)
    else:
        offset = 0
        fenced = False
        for number, line in enumerate(text.splitlines(keepends=True), start=1):
            content = line.rstrip("\r\n")
            heading = re.match(r"^(#{1,6})\s+(.+)$", content) if fmt == "markdown" and not fenced else None
            if fmt == "markdown" and re.match(r"^\s*(```|~~~)", content):
                fenced = not fenced
            kind = (
                "code"
                if fenced
                else "heading"
                if heading
                else "list_item"
                if fmt == "markdown" and re.match(r"^\s*(?:[-*+] |\d+\. )", content)
                else "paragraph"
            )
            b.add(
                kind,
                content,
                f"text/line/{number}",
                level=len(heading.group(1)) if heading else None,
                evidence=b.ref(
                    f"text/line/{number}",
                    line_start=number,
                    line_end=number,
                    char_start=offset,
                    char_end=offset + len(content),
                ),
            )
            offset += len(line)


def parse_document(
    data: bytes,
    filename: str,
    *,
    source_id: str,
    source_revision: str,
    limits: ParseLimits | None = None,
    engine: str = "basic",
) -> DocumentIR:
    limits = limits or ParseLimits()
    if not source_id or not source_revision:
        raise DocumentError("missing_source_version", "Source identity and immutable revision are required")
    fmt = detect_format(data, filename, limits)
    if fmt == "feishu_docx":
        if engine != "basic":
            raise DocumentError("parser_unavailable", "飞书原生快照使用块结构解析，不适用 Docling 文件解析")
        from ontology.documents.feishu_native import parse_native

        return parse_native(
            data, filename, source_id=source_id, source_revision=source_revision, limits=limits
        )
    if engine != "basic":
        if engine == "docling":
            from ontology.documents.structured import parse_docling

            return parse_docling(
                data, filename, source_id=source_id, source_revision=source_revision, limits=limits
            )
        raise DocumentError("parser_unavailable", "The requested parser is not enabled")
    ir = DocumentIR(
        source_id=source_id,
        source_revision=source_revision,
        filename=PurePosixPath(filename.replace("\\", "/")).name,
        content_hash=hashlib.sha256(data).hexdigest(),
        format=fmt,
        parser="basic/" + fmt,
        parser_version="2.0",
    )
    b = _Builder(ir, limits)
    try:
        if fmt == "docx":
            _docx(data, b)
        elif fmt == "pdf":
            _pdf(data, b)
        elif fmt == "xlsx":
            _xlsx(data, b)
        elif fmt == "pptx":
            from ontology.documents.slides import parse_slides

            parse_slides(data, b)
        elif fmt in {"png", "jpeg"}:
            from PIL import Image

            with Image.open(io.BytesIO(data)) as image:
                image.verify()
                b.add(
                    "image",
                    "",
                    "image/1",
                    evidence=b.ref("image/1", page_number=1),
                    metadata={"width": image.width, "height": image.height},
                )
                ir.pages.append(
                    PageStatus(
                        page_number=1,
                        status="needs_ocr",
                        width=image.width,
                        height=image.height,
                        reason="Requires visual model understanding",
                    )
                )
                ir.complete, ir.quality = False, "needs_review"
        else:
            _text(data, fmt, b)
    except DocumentError:
        raise
    except ImportError:
        raise DocumentError("parser_unavailable", "The selected parser dependency is not installed") from None
    except Exception:  # noqa: BLE001 -- decoder failures are classified without exposing source data.
        raise DocumentError("invalid_document", "Document structure cannot be decoded safely") from None
    return ir
