"""Reparse and immutable-history reads using the existing jobs and project lock."""

from typing import Literal
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Query, Request
from pydantic import Field
from sqlalchemy import func, select
from sqlalchemy.orm import defer

from ontology.content_access.service import content_context, prepare_content_access, require_document
from ontology.core.domain import exact_json_text, row_dict, scoped_record, wire_value
from ontology.core.errors import DomainError, conflict, missing
from ontology.core.schemas import StrictModel
from ontology.documents import DocumentError, ParseLimits
from ontology.documents.api import document_view
from ontology.documents.history import (
    active_parse_job,
    capture_current_parse,
    parse_payload,
    retained_parse_state,
    snapshot_as_document,
)
from ontology.documents.history_models import DocumentParseVersion
from ontology.documents.pages import file_digest
from ontology.documents.parser import detect_format_path
from ontology.documents.storage_models import Document
from ontology.identity.security import audit, authenticated
from ontology.jobs.models import Job
from ontology.search.content.units import ir_fingerprint, validate_document_ir
from ontology.search.objects.api import read_transaction

router = APIRouter(dependencies=[Depends(prepare_content_access)])


class ReparseInput(StrictModel):
    expected_revision: int = Field(ge=1)
    expected_parse_revision: int = Field(ge=0)
    expected_ir_fingerprint: str | None
    engine: Literal["auto", "basic", "docling"] = "auto"


def parse_view(row, doc, *, full=False):
    result = row_dict(row, exclude=() if full else ("ir",))
    result.update(
        filename=doc.filename,
        is_current=(row.source_revision, row.parse_revision, row.ir_fingerprint)
        == (
            doc.revision,
            doc.parse_revision,
            doc.ir_fingerprint,
        ),
    )
    if full:
        result["download_url"] = f"/api/documents/{doc.id}/download?" + urlencode(
            {
                "expected_revision": row.source_revision,
                "expected_hash": row.content_hash,
            }
        )
        # Preserve raw numeric evidence in a precise text channel for inspection.
        result["ir_json"] = exact_json_text(row.ir)
        result["ir"] = wire_value(result["ir"])
    return result


@router.post("/documents/{document_id}/reparse", status_code=202)
def reparse(document_id: str, data: ReparseInput, request: Request, user=Depends(authenticated)):
    with request.app.state.db.transaction(user.tenant_id, write=True) as session:
        doc = scoped_record(session, Document, document_id, user)
        ctx = content_context(session, request, user, doc.project_id, permission="documents:write")
        require_document(session, ctx, doc.id)
        session.refresh(doc)
        actual = ir_fingerprint(doc.ir) if retained_parse_state(doc) else doc.ir_fingerprint
        if (doc.revision, doc.parse_revision, actual) != (
            data.expected_revision,
            data.expected_parse_revision,
            data.expected_ir_fingerprint,
        ):
            conflict("解析基线已改变，请先刷新当前解析后再提交")
        if active_parse_job(session, doc):
            conflict("该文档已有活动解析任务，请先查看任务状态")
        path = request.app.state.settings.data_dir / "assets" / doc.storage_key
        if not path.is_file():
            raise DomainError(409, "asset_missing", "原件缺失，请先恢复原件")
        if path.stat().st_size != doc.size or file_digest(path) != doc.content_hash:
            conflict("原件校验失败，未创建解析任务")
        try:
            file_format = detect_format_path(
                path, doc.filename, ParseLimits(max_bytes=request.app.state.settings.max_upload_bytes)
            )
        except DocumentError as exc:
            raise DomainError(422, exc.code, str(exc)) from None
        engine = data.engine if data.engine != "auto" else "basic"
        if file_format == "feishu_docx" and engine != "basic":
            raise DomainError(422, "invalid_parser", "飞书原生快照仅支持块结构解析")
        capture_current_parse(session, doc)
        retained = retained_parse_state(doc)
        doc.state, doc.error = retained or "pending", None
        job = Job(
            tenant_id=doc.tenant_id,
            project_id=doc.project_id,
            kind="document.parse",
            owner_id=user.id,
            payload=parse_payload(doc, engine, file_format),
        )
        session.add(job)
        session.flush()
        audit(
            session,
            user,
            doc.project_id,
            "document.reparse.requested",
            doc.id,
            {
                "job_id": job.id,
                "engine": engine,
                "source_revision": doc.revision,
                "parse_revision": doc.parse_revision,
            },
        )
        return {"document": document_view(session, doc), "job": row_dict(job)}


@router.get("/documents/{document_id}/parses")
def parses(
    document_id: str,
    request: Request,
    limit: int = Query(25, ge=1, le=100),
    offset: int = Query(0, ge=0),
    user=Depends(authenticated),
):
    with read_transaction(request.app.state.db, user.tenant_id) as session:
        doc = session.scalar(select(Document).options(defer(Document.ir)).where(
            Document.id == document_id, Document.tenant_id == user.tenant_id,
        ))
        if doc is None:
            missing()
        ctx = content_context(session, request, user, doc.project_id)
        require_document(session, ctx, doc.id)
        conditions = (
            DocumentParseVersion.tenant_id == doc.tenant_id,
            DocumentParseVersion.project_id == doc.project_id,
            DocumentParseVersion.document_id == doc.id,
        )
        rows = session.scalars(
            select(DocumentParseVersion)
            .options(defer(DocumentParseVersion.ir))
            .where(*conditions)
            .order_by(
                DocumentParseVersion.source_revision.desc(),
                DocumentParseVersion.parse_revision.desc(),
            )
            .offset(offset)
            .limit(limit)
        ).all()
        return {
            "items": [parse_view(row, doc) for row in rows],
            "total": session.scalar(
                select(func.count()).select_from(DocumentParseVersion).where(*conditions)
            ),
        }


@router.get("/documents/{document_id}/parses/{parse_id}")
def parse_detail(document_id: str, parse_id: str, request: Request, user=Depends(authenticated)):
    with read_transaction(request.app.state.db, user.tenant_id) as session:
        doc = scoped_record(session, Document, document_id, user)
        ctx = content_context(session, request, user, doc.project_id)
        require_document(session, ctx, doc.id)
        session.refresh(doc)
        row = session.scalar(
            select(DocumentParseVersion).where(
                DocumentParseVersion.id == parse_id,
                DocumentParseVersion.tenant_id == doc.tenant_id,
                DocumentParseVersion.project_id == doc.project_id,
                DocumentParseVersion.document_id == doc.id,
            )
        )
        if not row:
            missing()
        if (row.source_revision, row.content_hash) != (doc.revision, doc.content_hash):
            conflict("历史解析对应原件当前不可用，未替换为新版原件")
        try:
            validate_document_ir(snapshot_as_document(row))
        except DocumentError:
            conflict("历史解析证据身份校验失败")
        if ir_fingerprint(row.ir) != row.ir_fingerprint:
            conflict("历史解析指纹校验失败")
        return parse_view(row, doc, full=True)
