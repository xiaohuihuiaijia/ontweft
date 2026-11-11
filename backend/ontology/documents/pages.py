"""Lazy, source-bound page inputs shared by review and model extraction.

PDF pages are rendered locally. Office units are slides, sheets or structural
blocks, never invented printed page numbers. No network calls or OCR live here.
"""

from __future__ import annotations

import base64
import hashlib
import io
import zipfile
from collections.abc import Iterator
from pathlib import Path

from ontology.documents.models import DocumentError, ParseLimits
from ontology.documents.parser import detect_format_path, parse_document


def file_digest(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def page_structure(path, filename, *, source_id, source_revision, ir=None, limits=None):
    """Reuse an interpretation and retain source images without guessing layout joins."""
    if not ir:
        return parse_document(
            path.read_bytes(), filename, source_id=source_id, source_revision=source_revision, limits=limits
        ).model_dump(mode="json")
    if ir.get("format") == "docx" and ir.get("parser", "").startswith("docling/") and "native_images" not in ir:
        native = parse_document(
            path.read_bytes(), filename, source_id=source_id, source_revision=source_revision, limits=limits
        )
        # Docling picture nodes have no archive-member identity. Native image
        # references are separate logical units; never pair them by position.
        return {**ir, "native_images": [e.model_dump(mode="json") for e in native.elements if e.kind == "image"]}
    return ir


def page_count(
    path: Path, filename: str, *, source_id: str, source_revision: str, ir: dict | None = None,
    limits: ParseLimits | None = None,
) -> int:
    """Count without rendering; callers can paginate before opening image inputs."""
    fmt = detect_format_path(path, filename, limits)
    if fmt == "pdf":
        import pypdfium2 as pdfium

        try:
            with pdfium.PdfDocument(str(path)) as pdf:
                return len(pdf)
        except (pdfium.PdfiumError, OSError, ValueError):
            raise DocumentError("invalid_pdf", "PDF cannot be opened") from None
    if fmt in {"png", "jpeg"}:
        return 1
    ir = page_structure(path, filename, source_id=source_id, source_revision=source_revision, ir=ir, limits=limits)
    return sum(1 for _ in _logical_units(ir))


def _png(data: bytes, max_edge: int) -> bytes:
    from PIL import Image, ImageOps

    try:
        with Image.open(io.BytesIO(data)) as original:
            image = ImageOps.exif_transpose(original).convert("RGB")
            image.thumbnail((max_edge, max_edge))
            output = io.BytesIO()
            image.save(output, "PNG")
            return output.getvalue()
    except (OSError, ValueError):
        raise DocumentError("invalid_image", "Image cannot be decoded") from None


def _data_url(data: bytes) -> str:
    return "data:image/png;base64," + base64.b64encode(data).decode("ascii")


def _pdf_images(page, max_edge, *, detail_images=False):
    """Render context first, then overlapping native-resolution details of the same page."""
    width, height = page.get_size()
    regions = [{"image_index": 0, "region": "overview", "rect": [0, 0, 1, 1]}]
    if detail_images:
        if height >= width:
            regions.extend(
                [
                    {"image_index": 1, "region": "top", "rect": [0, 0, 1, 0.54]},
                    {"image_index": 2, "region": "bottom", "rect": [0, 0.46, 1, 1]},
                ]
            )
        else:
            regions.extend(
                [
                    {"image_index": 1, "region": "left", "rect": [0, 0, 0.54, 1]},
                    {"image_index": 2, "region": "right", "rect": [0.46, 0, 1, 1]},
                ]
            )
    urls = []
    for region in regions:
        left, top, right, bottom = region["rect"]
        edge = min(max_edge, 1280 if region["image_index"] == 0 else 3200) if detail_images else max_edge
        scale = edge / max((right - left) * width, (bottom - top) * height)
        # PDFium crop uses distances from left/bottom/right/top after page rotation.
        bitmap = page.render(
            scale=scale, crop=(left * width, (1 - bottom) * height, (1 - right) * width, top * height)
        )
        try:
            image = bitmap.to_pil()
            output = io.BytesIO()
            image.save(output, "PNG")
            urls.append(_data_url(output.getvalue()))
            region.update(coordinate_system="normalized_page_TOPLEFT", pixels=list(image.size))
        finally:
            bitmap.close()
    return urls, regions


def _text(elements: list[dict]) -> str:
    parts = []
    for element in elements:
        cells = element.get("cells", [])
        if cells:
            parts.append("Table: " + element["evidence"]["element_path"])
            for cell in cells:
                address = cell.get("evidence", {}).get("cell_address")
                position = address or f"row={cell['row'] + 1}, column={cell['column'] + 1}"
                parts.append(
                    f"[{position}; span={cell.get('row_span', 1)}x{cell.get('column_span', 1)}] {cell.get('text', '')}"
                )
        elif element.get("text"):
            parts.append(element["text"])
    return "\n".join(parts)


def _logical_units(ir: dict) -> Iterator[tuple[str, list[dict]]]:
    elements = ir.get("source_elements") if ir.get("format") == "xlsx" else None
    elements = elements or ir.get("elements", [])
    native_images = ir.get("native_images")
    if native_images is not None:
        elements = [element for element in elements if element.get("kind") != "image"]
    if ir.get("format") == "pptx":
        for status in ir.get("pages", []):
            number = status["page_number"]
            yield f"幻灯片 {number}", [e for e in elements if e["evidence"].get("page_number") == number]
    elif ir.get("format") in {"xlsx", "csv"}:
        for element in elements:
            if element.get("kind") != "table":
                yield "内嵌图像", [element]
                continue
            cells = element.get("cells", [])
            rows = sorted({cell["row"] for cell in cells})
            sheet = element.get("metadata", {}).get("sheet", "表格")
            for start in range(0, max(1, len(rows)), 100):
                selected = set(rows[start : start + 100])
                # Keep original row indices and repeat the first row as context.
                if rows:
                    selected.add(rows[0])
                part = {**element, "cells": [c for c in cells if c["row"] in selected], "text": ""}
                label = (
                    f"{sheet} · 行 {rows[start] + 1}–{rows[min(start + 99, len(rows) - 1)] + 1}"
                    if rows
                    else sheet
                )
                yield label, [part]
    else:
        batch, length = [], 0
        for element in elements:
            size = max(
                len(element.get("text", "")), sum(len(c.get("text", "")) for c in element.get("cells", []))
            )
            if batch and (length + size > 24000 or len(batch) >= 40):
                yield "文档结构片段", batch
                batch, length = [], 0
            batch.append(element)
            length += size
        if batch or not elements:
            yield "文档结构片段", batch
    for element in native_images or []:
        yield "原生内嵌图像", [element]


def iter_page_inputs(
    path: Path,
    filename: str,
    *,
    source_id: str,
    source_revision: str,
    ir: dict | None = None,
    page_numbers: list[int] | None = None,
    image_max_edge: int = 2560,
    include_images: bool = True,
    detail_images: bool = False,
    verified_hash: str | None = None,
    limits: ParseLimits | None = None,
) -> Iterator[dict]:
    """Yield one input at a time. `page_number` is a logical-unit ID for Office.

    PDF/image evidence contains physical pages. Office block/cell evidence is
    unchanged. `image_data_urls` retains all embedded images; `image_data_url`
    is the first image for simple consumers. Callers must authorize the source.
    `verified_hash` is internal: supply only after checking this immutable file
    during the current request/run, to avoid hashing it again for every page.
    """
    if not 256 <= image_max_edge <= 8192:
        raise DocumentError("invalid_resolution", "Image edge must be between 256 and 8192 pixels")
    selected = set(page_numbers) if page_numbers is not None else None
    if selected is not None and any(number < 1 for number in selected):
        raise DocumentError("invalid_page", "Pages start at 1")
    fmt = detect_format_path(path, filename, limits)
    digest = verified_hash if verified_hash is not None else file_digest(path)
    common = {"source_id": source_id, "source_revision": source_revision, "content_hash": digest}
    found = set()
    if fmt == "pdf":
        import pypdfium2 as pdfium

        try:
            pdf = pdfium.PdfDocument(str(path))
        except (pdfium.PdfiumError, OSError, ValueError):
            raise DocumentError("invalid_pdf", "PDF cannot be opened") from None
        try:
            for index in range(len(pdf)):
                number = index + 1
                if selected is not None and number not in selected:
                    continue
                found.add(number)
                page = pdf[index]
                try:
                    textpage = page.get_textpage()
                    try:
                        text = textpage.get_text_range()
                    finally:
                        textpage.close()
                    urls, regions = [], []
                    if include_images:
                        urls, regions = _pdf_images(page, image_max_edge, detail_images=detail_images)
                    evidence = {**common, "element_path": f"pdf/page/{number}", "page_number": number}
                    elements = [
                        e
                        for e in (ir or {}).get("elements", [])
                        if e.get("evidence", {}).get("page_number") == number
                    ]
                    yield {
                        "page_number": number,
                        "label": f"第 {number} 页",
                        "text": text,
                        "elements": elements,
                        "status": "parsed" if text.strip() else "needs_vision",
                        "evidence": evidence,
                        "has_image": True,
                        "image_count": len(urls) if include_images else 1,
                        "image_data_url": urls[0] if urls else None,
                        "image_data_urls": urls,
                        "image_regions": regions,
                    }
                finally:
                    page.close()
        finally:
            pdf.close()
    elif fmt in {"png", "jpeg"}:
        if selected is None or 1 in selected:
            found.add(1)
            image = _data_url(_png(path.read_bytes(), image_max_edge)) if include_images else None
            yield {
                "page_number": 1,
                "label": "图像",
                "text": "",
                "elements": (ir or {}).get("elements", []),
                "status": "needs_vision",
                "has_image": True,
                "image_count": 1,
                "image_data_url": image,
                "image_data_urls": [image] if image else [],
                "evidence": {**common, "element_path": "image/1", "page_number": 1},
            }
    else:
        ir = page_structure(path, filename, source_id=source_id, source_revision=source_revision, ir=ir, limits=limits)
        archive = zipfile.ZipFile(path) if fmt in {"docx", "pptx", "xlsx"} else None
        try:
            for number, (label, elements) in enumerate(_logical_units(ir), 1):
                if selected is not None and number not in selected:
                    continue
                found.add(number)
                members = [
                    e["metadata"]["archive_member"]
                    for e in elements
                    if e.get("kind") == "image" and e.get("metadata", {}).get("archive_member")
                ]
                urls = []
                if archive and include_images:
                    for member in members:
                        try:
                            urls.append(_data_url(_png(archive.read(member), image_max_edge)))
                        except DocumentError:
                            # Unsupported vector objects remain explicitly reviewable.
                            raise DocumentError(
                                "image_conversion_required", "An embedded image requires conversion"
                            ) from None
                evidence = (
                    elements[0]["evidence"] if elements else {**common, "element_path": "document/empty"}
                )
                yield {
                    "page_number": number,
                    "label": label,
                    "text": _text(elements),
                    "elements": elements,
                    "status": "needs_vision" if members else "parsed",
                    "has_image": bool(members),
                    "image_count": len(members),
                    "image_data_url": urls[0] if urls else None,
                    "image_data_urls": urls,
                    "evidence": evidence,
                }
        finally:
            if archive:
                archive.close()
    if selected is not None and found != selected:
        raise DocumentError("invalid_page", "Requested page is outside the document")


def page_image(
    path: Path,
    filename: str,
    *,
    source_id: str,
    source_revision: str,
    page_number: int,
    ir: dict | None = None,
    image_index: int = 0,
    max_edge: int = 2560,
    verified_hash: str | None = None,
    limits: ParseLimits | None = None,
) -> bytes:
    pages = iter_page_inputs(
        path,
        filename,
        source_id=source_id,
        source_revision=source_revision,
        ir=ir,
        page_numbers=[page_number],
        image_max_edge=max_edge,
        verified_hash=verified_hash,
        limits=limits,
    )
    try:
        page = next(pages)
        images = page["image_data_urls"]
        if not 0 <= image_index < len(images):
            raise DocumentError("page_has_no_image", "This logical page has no requested image")
        return base64.b64decode(images[image_index].split(",", 1)[1])
    except StopIteration:
        raise DocumentError("invalid_page", "Requested page is outside the document") from None
    finally:
        pages.close()
