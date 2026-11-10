"""Immutable parse evidence and source-bound, reproducible parse requests."""

import os
from copy import deepcopy
from importlib.metadata import PackageNotFoundError, version
from types import SimpleNamespace

from sqlalchemy import select

from ontology.core.errors import DomainError, conflict
from ontology.documents.history_models import DocumentParseVersion
from ontology.documents.models import DocumentError
from ontology.documents.storage_models import Document
from ontology.jobs.models import Job
from ontology.search.content.units import ir_fingerprint, validate_document_ir

ACTIVE_PARSE_STATUSES = ("pending", "running", "retry_wait")


def current_parse(session, doc):
    return session.scalar(
        select(DocumentParseVersion).where(
            DocumentParseVersion.tenant_id == doc.tenant_id,
            DocumentParseVersion.project_id == doc.project_id,
            DocumentParseVersion.document_id == doc.id,
            DocumentParseVersion.source_revision == doc.revision,
            DocumentParseVersion.parse_revision == doc.parse_revision,
        )
    )


def retained_parse_state(doc):
    if not doc.ir:
        return None
    try:
        ir = validate_document_ir(doc)
        if doc.ir_fingerprint and doc.ir_fingerprint != ir_fingerprint(doc.ir):
            return None
        return "ready" if ir.complete else "partial"
    except DocumentError:
        return None


def capture_current_parse(session, doc, *, job=None):
    if not doc.ir:
        return None
    try:
        ir = validate_document_ir(doc)
        fingerprint = ir_fingerprint(doc.ir)
    except DocumentError:
        raise DomainError(
            409, "invalid_parse_evidence", "当前解析证据不完整或身份不符，请先核对原件"
        ) from None
    if doc.ir_fingerprint and doc.ir_fingerprint != fingerprint:
        conflict("当前解析指纹不一致，不能覆盖证据")
    existing = current_parse(session, doc)
    if existing:
        if existing.ir_fingerprint != fingerprint or existing.content_hash != doc.content_hash:
            conflict("同一解析版本已有不同证据，不能覆盖历史")
        return existing
    doc.ir_fingerprint = fingerprint
    configuration = job.payload.get("configuration", {}) if job else {}
    row = DocumentParseVersion(
        tenant_id=doc.tenant_id,
        project_id=doc.project_id,
        document_id=doc.id,
        source_revision=doc.revision,
        parse_revision=doc.parse_revision,
        content_hash=doc.content_hash,
        ir_fingerprint=fingerprint,
        ir=deepcopy(doc.ir),
        parser=ir.parser,
        parser_version=ir.parser_version,
        engine=job.payload["engine"] if job else "legacy",
        origin="worker" if job else "legacy",
        job_id=job.id if job else None,
        configuration=deepcopy(configuration),
        configuration_fingerprint=job.payload.get("configuration_fingerprint", ir_fingerprint({}))
        if job
        else ir_fingerprint({}),
    )
    session.add(row)
    session.flush()
    return row


def active_parse_job(session, doc, *, exclude=None):
    query = select(Job).where(
        Job.tenant_id == doc.tenant_id,
        Job.project_id == doc.project_id,
        Job.kind == "document.parse",
        Job.payload["document_id"].as_string() == doc.id,
        Job.status.in_(ACTIVE_PARSE_STATUSES),
    )
    if exclude:
        query = query.where(Job.id != exclude)
    return session.scalar(query.order_by(Job.created_at, Job.id).limit(1))


def check_parse_baseline(doc, job):
    data = job.payload
    if not doc or (doc.tenant_id, doc.project_id, doc.id, doc.revision) != (
        job.tenant_id,
        job.project_id,
        data.get("document_id"),
        data.get("revision"),
    ):
        conflict("解析任务的原件身份或版本已改变")
    if "expected_parse_revision" not in data:
        # Pre-upgrade initial jobs have no previous result to overwrite.
        if doc.parse_revision != 0 or doc.ir:
            conflict("旧解析任务未固定解析基线，请重新提交解析")
    elif (doc.parse_revision, doc.ir_fingerprint) != (
        data["expected_parse_revision"],
        data.get("expected_ir_fingerprint"),
    ):
        conflict("已有新解析结果，旧任务不能覆盖当前版本")


def parse_configuration(engine, file_format):
    # Freeze the selected parsing path's direct dependencies, including our
    # safety preflight and XLSX native-cell pass. This is not a transitive lock.
    packages = ("pydantic",)
    if engine == "docling":
        packages += ("docling-slim", "docling-core")
        packages += {
            "docx": ("defusedxml", "python-docx", "lxml", "Pillow"),
            "xlsx": ("defusedxml", "openpyxl", "lxml", "Pillow"),
            "pdf": ("pypdf", "pypdfium2", "Pillow", "Rtree"),
        }.get(file_format, ())
        if file_format == "pdf" and os.getenv("ONTOLOGY_DOCLING_PDF_MODE", "layout") == "layout":
            packages += ("numpy", "docling-ibm-models", "torch", "transformers")
    else:
        packages += {
            "docx": ("defusedxml",),
            "xlsx": ("defusedxml", "openpyxl"),
            "pdf": ("pypdf",),
        }.get(file_format, ())
    versions = {}
    for package in packages:
        try:
            versions[package] = version(package)
        except PackageNotFoundError:
            versions[package] = "unavailable"
    public = {"engine": engine, "format": file_format, "adapter": "document-parser-v1", "packages": versions}
    if file_format == "feishu_docx":
        from ontology.documents.feishu_native import PARSER_VERSION, SNAPSHOT_VERSION

        public.update(adapter="feishu-native/" + PARSER_VERSION, snapshot_version=SNAPSHOT_VERSION)
    identity = dict(public)
    if engine == "docling" and file_format == "pdf":
        public.update(
            pdf_mode=os.getenv("ONTOLOGY_DOCLING_PDF_MODE", "layout"),
            model_manifest_sha256=os.getenv("ONTOLOGY_DOCLING_MANIFEST_SHA256") or None,
        )
        identity = {**public, "artifacts_path": os.getenv("ONTOLOGY_DOCLING_ARTIFACTS_PATH") or None}
    return public, ir_fingerprint(identity)


def parse_payload(doc, engine, file_format):
    configuration, fingerprint = parse_configuration(engine, file_format)
    return {
        "document_id": doc.id,
        "revision": doc.revision,
        "engine": engine,
        "expected_parse_revision": doc.parse_revision,
        "expected_ir_fingerprint": doc.ir_fingerprint,
        "configuration": configuration,
        "configuration_fingerprint": fingerprint,
    }


def validate_job_configuration(job):
    data = job.payload
    if "configuration" not in data:
        return  # Explicitly legacy initial jobs; not a reproducible-cache claim.
    actual, fingerprint = parse_configuration(data["engine"], data["configuration"]["format"])
    if actual != data["configuration"] or fingerprint != data.get("configuration_fingerprint"):
        conflict("解析环境或配置已改变，请重新提交解析任务；未自动替换配置")


def snapshot_as_document(snapshot):
    return SimpleNamespace(
        id=snapshot.document_id,
        revision=snapshot.source_revision,
        content_hash=snapshot.content_hash,
        ir=snapshot.ir,
    )


def bound_snapshot(session, project, reference):
    if not reference.get("parse_id"):
        return None
    row = session.scalar(
        select(DocumentParseVersion).where(
            DocumentParseVersion.tenant_id == project.tenant_id,
            DocumentParseVersion.project_id == project.id,
            DocumentParseVersion.id == reference["parse_id"],
            DocumentParseVersion.document_id == reference.get("document_id"),
        )
    )
    if not row:
        return None
    expected = {
        "revision": row.source_revision,
        "parse_revision": row.parse_revision,
        "ir_fingerprint": row.ir_fingerprint,
        "content_hash": row.content_hash,
    }
    if any(reference.get(key) != value for key, value in expected.items()):
        return None
    return row


def reference_paths(ir):
    paths = set()
    for element in ir.get("elements", []) + ir.get("source_elements", []):
        paths.add(element.get("evidence", {}).get("element_path"))
        paths.update(cell.get("evidence", {}).get("element_path") for cell in element.get("cells", []))
    return paths


def canonical_references(session, ctx, references, *, capture=True):
    result = []
    keys = {
        "document_id",
        "revision",
        "element_path",
        "parse_id",
        "parse_revision",
        "ir_fingerprint",
        "content_hash",
    }
    for ref in references:
        if set(ref) - keys:
            raise DomainError(422, "invalid_reference", "引用包含未知字段")
        doc = session.scalar(
            select(Document).where(
                Document.tenant_id == ctx.project.tenant_id,
                Document.project_id == ctx.project.id,
                Document.id == ref.get("document_id"),
            )
        )
        if not doc or ref.get("revision") != doc.revision:
            raise DomainError(422, "invalid_reference", "引用原件不可用或版本不匹配")
        if ref.get("parse_id"):
            snapshot = bound_snapshot(session, ctx.project, ref)
        else:
            if not capture or "parse_revision" not in ref or not ref.get("ir_fingerprint"):
                raise DomainError(422, "legacy_unbound", "引用未固定解析版本，请重新选择来源并保存新知识修订")
            actual = ir_fingerprint(doc.ir) if doc.ir else None
            if (ref["parse_revision"], ref["ir_fingerprint"], ref.get("content_hash")) != (
                doc.parse_revision,
                actual,
                doc.content_hash,
            ):
                conflict("预览后的解析版本已改变，请重新选择引用来源")
            if doc.state != "ready":
                raise DomainError(422, "invalid_reference", "当前解析不完整，不能发布为知识引用")
            snapshot = capture_current_parse(session, doc)
        if not snapshot or snapshot.content_hash != doc.content_hash or not snapshot.ir.get("complete"):
            raise DomainError(422, "invalid_reference", "固定解析不可用、原件不匹配或解析不完整")
        if ref.get("element_path") and ref["element_path"] not in reference_paths(snapshot.ir):
            raise DomainError(422, "invalid_reference", "固定解析中的引用位置不存在")
        result.append(
            {
                "document_id": doc.id,
                "revision": snapshot.source_revision,
                "parse_id": snapshot.id,
                "parse_revision": snapshot.parse_revision,
                "ir_fingerprint": snapshot.ir_fingerprint,
                "content_hash": snapshot.content_hash,
                **({"element_path": ref["element_path"]} if ref.get("element_path") else {}),
            }
        )
    return result


def reference_status(session, project, ref):
    if not ref.get("parse_id"):
        return {**ref, "status": "legacy_unbound"}
    snapshot = bound_snapshot(session, project, ref)
    doc = session.scalar(
        select(Document).where(
            Document.tenant_id == project.tenant_id,
            Document.project_id == project.id,
            Document.id == ref.get("document_id"),
        )
    )
    valid = (
        snapshot
        and doc
        and (doc.revision, doc.content_hash) == (snapshot.source_revision, snapshot.content_hash)
    )
    if valid and ref.get("element_path"):
        valid = ref["element_path"] in reference_paths(snapshot.ir)
    return {**ref, "status": "bound" if valid else "unavailable"}
