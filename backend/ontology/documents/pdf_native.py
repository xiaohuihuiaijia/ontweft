"""PDFium native text, read directly from immutable files in bounded page batches."""

from contextlib import closing
from importlib.metadata import version
from pathlib import Path

from ontology.documents.models import DocumentError, DocumentIR, PageStatus, ParseLimits


def extract_pages(source, builder, *, start_page=1, page_limit=None):
    import pypdfium2 as pdfium

    try:
        pdf = pdfium.PdfDocument(source)
    except pdfium.PdfiumError as exc:
        if exc.err_code == pdfium.raw.FPDF_ERR_PASSWORD:
            raise DocumentError(
                "encrypted", "Encrypted PDF requires an explicit authorized decryption step"
            ) from None
        raise DocumentError("invalid_pdf", "PDF cannot be opened") from None
    with pdf:
        if pdfium.raw.FPDF_GetSecurityHandlerRevision(pdf) >= 0:
            raise DocumentError("encrypted", "Encrypted PDF requires an explicit authorized decryption step")
        total = len(pdf)
        if total > builder.limits.max_pages:
            raise DocumentError("resource_limit", "PDF exceeds the page budget")
        if not total or start_page < 1 or start_page > total:
            raise DocumentError("invalid_page", "PDF page range is invalid")
        end = min(total, start_page - 1 + page_limit) if page_limit is not None else total
        for number in range(start_page, end + 1):
            status = PageStatus(page_number=number, status="parsed")
            try:
                with closing(pdf[number - 1]) as page:
                    status.width, status.height = page.get_size()
                    status.rotation = page.get_rotation()
                    with closing(page.get_textpage()) as textpage:
                        text = textpage.get_text_range() or ""
                    if not text.strip():
                        status.status, status.reason = (
                            "needs_ocr",
                            "No extractable native text; visual understanding required",
                        )
                    builder.add(
                        "paragraph",
                        text,
                        f"pdf/page/{number}",
                        evidence=builder.ref(f"pdf/page/{number}", page_number=number),
                    )
            except DocumentError:
                raise
            except (pdfium.PdfiumError, OSError, ValueError):
                status.status, status.reason = "failed", "Native page text extraction failed"
            if status.status != "parsed":
                builder.ir.complete, builder.ir.quality = False, "needs_review"
            builder.ir.pages.append(status)
        builder.ir.warnings.append(
            "Basic PDF extracts native page text only: no verified reading order, table cells, bounding boxes or visual understanding"
        )
        builder.ir.parser_version = "pdfium/" + version("pypdfium2")
        return {"page_count": total, "next_page": end + 1 if end < total else None}


def parse_pdf_batch(
    path: Path,
    filename,
    *,
    source_id,
    source_revision,
    content_hash,
    start_page=1,
    page_limit=32,
    limits=None,
):
    from ontology.documents.parser import _Builder, detect_format_path

    limits = limits or ParseLimits()
    if detect_format_path(path, filename, limits) != "pdf":
        raise DocumentError("format_mismatch", "PDF batch requires a PDF source")
    ir = DocumentIR(
        source_id=source_id,
        source_revision=source_revision,
        filename=Path(filename).name,
        content_hash=content_hash,
        format="pdf",
        parser="basic/pdf",
        parser_version="3.0",
    )
    progress = extract_pages(path, _Builder(ir, limits), start_page=start_page, page_limit=page_limit)
    return {"ok": True, "ir": ir.model_dump(mode="json"), **progress}
