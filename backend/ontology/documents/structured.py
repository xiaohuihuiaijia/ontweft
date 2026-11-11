"""Pinned offline Docling adapter; run only in the isolated parsing worker.

No automatic model download, remote service, external resource fetch or implicit
parser fallback. The socket guard supplements OS/container egress restrictions.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import socket
import threading
from contextlib import contextmanager
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field, ValidationError

from ontology.documents.models import (
    DocumentError,
    DocumentIR,
    EvidenceRef,
    PageStatus,
    ParseLimits,
    TableCell,
)

SUPPORTED_DOCLING = "2.143.1"
_conversion_lock = threading.RLock()


class DoclingOptions(BaseModel):
    pdf_mode: Literal["layout", "native"] = "layout"
    artifacts_path: Path | None = None
    artifacts_manifest_sha256: str | None = None
    document_timeout: float = Field(default=120, gt=0, le=600)
    num_threads: int = Field(default=2, ge=1, le=8)

    @classmethod
    def from_environment(cls) -> DoclingOptions:
        try:
            return cls(
                pdf_mode=os.getenv("ONTOLOGY_DOCLING_PDF_MODE", "layout"),
                artifacts_path=os.getenv("ONTOLOGY_DOCLING_ARTIFACTS_PATH") or None,
                artifacts_manifest_sha256=os.getenv("ONTOLOGY_DOCLING_MANIFEST_SHA256") or None,
            )
        except ValidationError:
            raise DocumentError(
                "invalid_parser_config", "Docling deployment configuration is invalid"
            ) from None


def _validate_artifacts(options: DoclingOptions) -> Path:
    """Check deployment-pinned artifact hashes, not a user supplied 'approved' flag."""
    if options.artifacts_path is None or options.artifacts_manifest_sha256 is None:
        raise DocumentError(
            "parser_not_configured",
            "Layout PDF requires an offline artifact directory and pinned manifest digest",
        )
    try:
        root = options.artifacts_path.resolve(strict=True)
        manifest = root / "manifest.json"
        if not root.is_dir() or manifest.stat().st_size > 4 * 1024 * 1024:
            raise ValueError()
        payload = manifest.read_bytes()
        if hashlib.sha256(payload).hexdigest() != options.artifacts_manifest_sha256:
            raise DocumentError("artifact_integrity", "Offline manifest differs from its deployment pin")
        data = json.loads(payload)
        files = data["files"]
        if (
            data.get("docling_version") != SUPPORTED_DOCLING
            or not isinstance(files, dict)
            or not files
            or len(files) > 20000
        ):
            raise ValueError()
        verified = set()
        for relative, expected in files.items():
            path = (root / relative).resolve(strict=True)
            if not path.is_relative_to(root) or not path.is_file() or path == manifest:
                raise ValueError()
            if not isinstance(expected, str) or len(expected) != 64:
                raise ValueError()
            digest = hashlib.sha256()
            with path.open("rb") as stream:
                for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                    digest.update(chunk)
            if digest.hexdigest() != expected:
                raise DocumentError("artifact_integrity", "Offline model artifact digest does not match")
            verified.add(path)
        if any(p.resolve() not in verified for p in root.rglob("*") if p.is_file() and p != manifest):
            raise DocumentError("artifact_integrity", "Offline bundle contains unlisted files")
        return root
    except DocumentError:
        raise
    except (OSError, ValueError, KeyError, TypeError):
        raise DocumentError(
            "artifact_integrity", "Offline artifact bundle or manifest is incomplete"
        ) from None


@contextmanager
def offline_docling():
    """Serialize conversion and deny sockets in model threads as well as the caller."""

    def denied(*args: Any, **kwargs: Any) -> Any:
        raise DocumentError("network_disabled", "Network access is disabled in the document parser")

    flags = dict.fromkeys(
        (
            "HF_HUB_OFFLINE",
            "TRANSFORMERS_OFFLINE",
            "HF_DATASETS_OFFLINE",
            "HF_HUB_DISABLE_TELEMETRY",
            "DO_NOT_TRACK",
        ),
        "1",
    )
    with _conversion_lock:
        old_flags = {name: os.environ.get(name) for name in flags}
        changed = [
            (socket, "create_connection"),
            (socket, "getaddrinfo"),
            (socket.socket, "connect"),
            (socket.socket, "connect_ex"),
            (socket.socket, "sendto"),
        ]
        originals = [(owner, name, getattr(owner, name)) for owner, name in changed]
        try:
            os.environ.update(flags)
            for owner, name in changed:
                setattr(owner, name, denied)
            yield
        finally:
            for owner, name, original in originals:
                setattr(owner, name, original)
            for name, old in old_flags.items():
                if old is None:
                    os.environ.pop(name, None)
                else:
                    os.environ[name] = old


def _preflight(data: bytes, fmt: str, limits: ParseLimits) -> list[PageStatus]:
    from ontology.documents.parser import _xml, _zip

    if fmt in {"docx", "xlsx"}:
        with _zip(data, limits) as archive:
            for member in archive.namelist():
                if member.endswith((".xml", ".rels")):
                    _xml(archive.read(member))
        return []
    from pypdf import PdfReader

    try:
        pdf = PdfReader(io.BytesIO(data), strict=True)
        if pdf.is_encrypted:
            raise DocumentError("encrypted", "Encrypted PDF requires authorized decryption")
        if len(pdf.pages) > limits.max_pages:
            raise DocumentError("resource_limit", "PDF exceeds the page budget")
        return [
            PageStatus(
                page_number=i + 1,
                status="failed",
                width=float(page.mediabox.width),
                height=float(page.mediabox.height),
                rotation=int(page.get("/Rotate", 0)),
                reason="No parser result for this page",
            )
            for i, page in enumerate(pdf.pages)
        ]
    except DocumentError:
        raise
    except Exception:  # noqa: BLE001 -- classify untrusted PDF decoder errors.
        raise DocumentError("invalid_pdf", "PDF cannot be inspected safely") from None


def _convert(
    data: bytes, filename: str, fmt: str, limits: ParseLimits, options: DoclingOptions, artifacts: Path | None
) -> tuple[Any, str, list[str]]:
    # Direct backends avoid DocumentConverter's unrelated eager ASR/torch imports.
    from docling.datamodel.backend_options import (
        MsExcelBackendOptions,
        MsWordBackendOptions,
        PdfBackendOptions,
    )
    from docling.datamodel.base_models import InputFormat
    from docling.datamodel.document import InputDocument
    from docling.datamodel.settings import DocumentLimits

    source = io.BytesIO(data)
    input_limits = DocumentLimits(max_num_pages=limits.max_pages, max_file_size=limits.max_bytes)
    if fmt in {"docx", "xlsx"}:
        if fmt == "docx":
            from docling.backend.msword_backend import MsWordDocumentBackend

            backend, input_format = MsWordDocumentBackend, InputFormat.DOCX
            backend_options = MsWordBackendOptions(
                enable_remote_fetch=False,
                enable_local_fetch=False,
                render_chart_images=False,
                max_total_bytes=limits.max_expanded_bytes,
                max_file_bytes=limits.max_expanded_bytes,
                max_member_count=limits.max_archive_entries,
            )
        else:
            from docling.backend.msexcel_backend import MsExcelDocumentBackend

            backend, input_format = MsExcelDocumentBackend, InputFormat.XLSX
            backend_options = MsExcelBackendOptions(
                enable_remote_fetch=False, enable_local_fetch=False, render_chart_images=False
            )
        in_doc = InputDocument(
            source,
            input_format,
            backend,
            backend_options=backend_options,
            filename=filename,
            limits=input_limits,
        )
        try:
            if not in_doc.valid:
                raise DocumentError("invalid_document", "Docling rejected the Office document")
            return in_doc._backend.convert(), "success", []
        finally:
            if in_doc._backend:
                in_doc._backend.unload()
    from docling.backend.pypdfium2_backend import PyPdfiumDocumentBackend
    from docling.datamodel.accelerator_options import AcceleratorDevice, AcceleratorOptions
    from docling.datamodel.pipeline_options import NativePdfPipelineOptions, PdfPipelineOptions
    from docling.pipeline.native_pdf_pipeline import NativePdfPipeline

    common = {
        "enable_remote_services": False,
        "allow_external_plugins": False,
        "document_timeout": options.document_timeout,
        "do_picture_classification": False,
        "do_picture_description": False,
        "do_chart_extraction": False,
        "accelerator_options": AcceleratorOptions(
            device=AcceleratorDevice.CPU, num_threads=options.num_threads
        ),
    }
    if options.pdf_mode == "native":
        pipeline = NativePdfPipeline(
            NativePdfPipelineOptions(
                **common, generate_page_images=False, generate_picture_images=False, parser_threads=1
            )
        )
    else:
        from docling.pipeline.standard_pdf_pipeline import StandardPdfPipeline

        pipeline = StandardPdfPipeline(
            PdfPipelineOptions(
                **common,
                artifacts_path=artifacts,
                do_ocr=False,
                do_table_structure=True,
                do_formula_enrichment=False,
                do_code_enrichment=False,
                generate_page_images=False,
                generate_picture_images=False,
            )
        )
    in_doc = InputDocument(
        source,
        InputFormat.PDF,
        PyPdfiumDocumentBackend,
        backend_options=PdfBackendOptions(enable_remote_fetch=False, enable_local_fetch=False),
        filename=filename,
        limits=input_limits,
    )
    try:
        if not in_doc.valid:
            raise DocumentError("invalid_document", "Docling rejected the PDF")
        result = pipeline.execute(in_doc, raises_on_error=False)
        errors = [str(getattr(error, "category", "conversion_error")) for error in result.errors]
        return result.document, result.status.value, errors
    finally:
        if in_doc._backend:
            in_doc._backend.unload()


def _provenance(builder: Any, path: str, items: list[Any], *, cell_bbox: Any = None) -> list[EvidenceRef]:
    refs = []
    for prov in items:
        box = cell_bbox or getattr(prov, "bbox", None)
        span = getattr(prov, "charspan", None)
        refs.append(
            builder.ref(
                path,
                page_number=prov.page_no,
                bbox=(box.l, box.t, box.r, box.b) if box else None,
                coordinate_system=f"pdf_points_{box.coord_origin.value}" if box else None,
                char_start=span[0] if span else None,
                char_end=span[1] if span else None,
            )
        )
    return refs


def _document_ir(
    document: Any,
    *,
    data: bytes,
    filename: str,
    fmt: str,
    limits: ParseLimits,
    source_id: str,
    source_revision: str,
    mode: str,
    status: str,
    page_statuses: list[PageStatus],
    errors: list[str],
) -> DocumentIR:
    from ontology.documents.parser import _Builder

    ir = DocumentIR(
        source_id=source_id,
        source_revision=source_revision,
        filename=Path(filename).name,
        content_hash=hashlib.sha256(data).hexdigest(),
        format=fmt,
        parser="docling/" + (mode if fmt == "pdf" else fmt),
        parser_version=version("docling-slim"),
        quality="basic" if fmt == "pdf" and mode == "native" else "structured",
        complete=status == "success",
        pages=page_statuses,
    )
    builder = _Builder(ir, limits)
    images = 0
    pages_with_text = set()
    for item, level in document.iterate_items(with_groups=False, traverse_pictures=True):
        label = item.label.value
        path = "docling/" + item.self_ref
        provenance = _provenance(builder, path, getattr(item, "prov", [])) if fmt == "pdf" else []
        evidence = provenance[0] if provenance else builder.ref(path)
        metadata = {
            "docling_ref": item.self_ref,
            "docling_label": label,
            "hierarchy_level": level,
            "content_layer": item.content_layer.value,
            "parent_ref": item.parent.cref if item.parent else None,
        }
        text = getattr(item, "text", "")
        if label == "table":
            cells = []
            table_prov = getattr(item, "prov", [])
            cell_page_is_known = len({p.page_no for p in table_prov}) == 1
            if fmt == "pdf" and len({p.page_no for p in table_prov}) > 1:
                metadata["cell_page_mapping"] = "unavailable"
                ir.warnings.append(
                    "A multi-page table has no verified cell-to-page mapping; cell coordinates are omitted"
                )
            for index, cell in enumerate(item.data.table_cells):
                cell_path = f"{path}/cell/{index}"
                refs = (
                    _provenance(builder, cell_path, table_prov, cell_bbox=cell.bbox)
                    if fmt == "pdf" and cell_page_is_known
                    else []
                )
                cell_evidence = refs[0] if refs else builder.ref(cell_path)
                cell_evidence = cell_evidence.model_copy(update={"char_start": None, "char_end": None})
                # A table's box is not its cell's box.
                if cell.bbox is None and refs:
                    cell_evidence = cell_evidence.model_copy(update={"bbox": None, "coordinate_system": None})
                cells.append(
                    TableCell(
                        row=cell.start_row_offset_idx,
                        column=cell.start_col_offset_idx,
                        row_span=cell.row_span,
                        column_span=cell.col_span,
                        text=cell.text,
                        evidence=cell_evidence,
                        column_header=cell.column_header,
                        row_header=cell.row_header,
                    )
                )
            text = "\n".join(cell.text for cell in cells)
            builder.add(
                "table",
                text,
                path,
                evidence=evidence,
                provenance=provenance,
                cells=cells,
                rows=item.data.num_rows,
                columns=item.data.num_cols,
                metadata=metadata,
            )
        elif label in {"picture", "chart"}:
            if images >= limits.max_images:
                ir.complete, ir.quality = False, "needs_review"
                if "Image budget reached; subsequent text was still processed" not in ir.warnings:
                    ir.warnings.append("Image budget reached; subsequent text was still processed")
                continue
            images += 1
            ir.complete, ir.quality = False, "needs_review"
            image = getattr(item, "image", None)
            if image is not None:
                metadata["width"], metadata["height"] = image.size.width, image.size.height
            builder.add("image", "", path, evidence=evidence, provenance=provenance, metadata=metadata)
        else:
            kind = {
                "section_header": "heading",
                "title": "heading",
                "list_item": "list_item",
                "code": "code",
                "formula": "formula",
            }.get(label, "paragraph")
            builder.add(
                kind,
                text,
                path,
                level=getattr(item, "level", None),
                evidence=evidence,
                provenance=provenance,
                metadata=metadata,
            )
        if text.strip():
            pages_with_text.update(ref.page_number for ref in provenance)
    if fmt == "pdf":
        for page in ir.pages:
            parsed = document.pages.get(page.page_number)
            if parsed is not None:
                page.width, page.height = parsed.size.width, parsed.size.height
                if page.page_number in pages_with_text:
                    page.status, page.reason = "parsed", None
                else:
                    page.status, page.reason = "needs_ocr", "No native text; OCR is disabled in this profile"
            if page.status != "parsed":
                ir.complete, ir.quality = False, "needs_review"
        ir.warnings.append(
            "Explicit native PDF profile: boxes/text cells only; no learned layout, tables or OCR"
            if mode == "native"
            else "Layout/table models ran offline; OCR and formula enrichment are disabled"
        )
    else:
        ir.warnings.append(
            "Office evidence uses version-bound Docling references; no physical Word page numbers are inferred"
        )
    if errors:
        ir.warnings.append("Docling conversion reported: " + ", ".join(sorted(set(errors))))
        ir.complete, ir.quality = False, "needs_review"
    if status != "success":
        ir.warnings.append("Docling status: " + status)
        ir.quality = "needs_review"
    if fmt == "xlsx":
        # Docling's data-only workbook interpretation can omit uncached formula
        # cells. Preserve a separate authoritative native-cell layer explicitly.
        from ontology.documents.parser import parse_document

        native = parse_document(
            data,
            filename,
            source_id=source_id,
            source_revision=source_revision,
            limits=limits,
            engine="basic",
        )
        ir.source_elements = native.elements
        all_elements = ir.elements + ir.source_elements
        if (
            len(all_elements) > limits.max_elements
            or sum(len(element.text) for element in all_elements) > limits.max_characters
            or sum(len(element.cells) for element in all_elements) > limits.max_cells
        ):
            raise DocumentError(
                "resource_limit", "Combined document interpretation and source evidence exceed the budget"
            )
        if not native.complete:
            ir.complete, ir.quality = False, "needs_review"
        ir.warnings.extend(native.warnings)
        ir.warnings.append(
            "Native worksheet cells are retained in source_elements, including formulas and cached values; inferred tables are a separate interpretation"
        )
    return ir


def parse_docling(
    data: bytes,
    filename: str,
    *,
    source_id: str,
    source_revision: str,
    limits: ParseLimits | None = None,
    options: DoclingOptions | None = None,
) -> DocumentIR:
    from ontology.documents.parser import detect_format

    limits = limits or ParseLimits()
    if not source_id or not source_revision:
        raise DocumentError("invalid_source", "Document evidence requires source identity and revision")
    options = options or DoclingOptions.from_environment()
    fmt = detect_format(data, filename, limits)
    if fmt not in {"docx", "xlsx", "pdf"}:
        raise DocumentError("unsupported_format", "The enabled Docling profile accepts DOCX, XLSX and PDF")
    try:
        if version("docling-slim") != SUPPORTED_DOCLING:
            raise DocumentError(
                "parser_version_mismatch", "Docling version differs from the reviewed adapter pin"
            )
    except PackageNotFoundError:
        raise DocumentError("parser_unavailable", "The pinned Docling subset is not installed") from None
    pages = _preflight(data, fmt, limits)
    artifacts = _validate_artifacts(options) if fmt == "pdf" and options.pdf_mode == "layout" else None
    with offline_docling():
        try:
            document, status, errors = _convert(data, filename, fmt, limits, options, artifacts)
            if status not in {"success", "partial_success"}:
                raise DocumentError("conversion_failed", "Docling could not convert the document")
            return _document_ir(
                document,
                data=data,
                filename=filename,
                fmt=fmt,
                limits=limits,
                source_id=source_id,
                source_revision=source_revision,
                mode=options.pdf_mode,
                status=status,
                page_statuses=pages,
                errors=errors,
            )
        except DocumentError:
            raise
        except ImportError:
            raise DocumentError(
                "parser_unavailable", "This Docling profile needs additional reviewed dependencies"
            ) from None
        except Exception:  # noqa: BLE001 -- redact untrusted decoder/runtime errors.
            raise DocumentError(
                "conversion_failed", "Docling failed without changing parser profiles"
            ) from None
