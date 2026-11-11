import hashlib
from pathlib import Path
from urllib.parse import quote, urlencode

from fastapi import APIRouter, Depends, File, Form, Query, Request, UploadFile
from fastapi.responses import FileResponse, Response
from sqlalchemy import func, select
from sqlalchemy.orm import defer, load_only

from ontology.content_access.models import DocumentSource
from ontology.content_access.service import (
    content_context,
    document_condition,
    prepare_content_access,
    require_document,
)
from ontology.core.domain import row_dict, scoped_record
from ontology.core.errors import DomainError, conflict, missing
from ontology.core.models import uid
from ontology.documents.history import (
    ACTIVE_PARSE_STATUSES,
    active_parse_job,
    current_parse,
    parse_payload,
    retained_parse_state,
)
from ontology.documents.history_models import DocumentParseVersion
from ontology.documents.storage_models import Document
from ontology.identity.security import access, audit, authenticated
from ontology.jobs.models import Job

router = APIRouter(dependencies=[Depends(prepare_content_access)])


def document_view(session, row, *, summary=False):
    from ontology.search.content.units import ir_fingerprint

    result = row_dict(row, exclude=("storage_key", "ir") if summary else ("storage_key",))
    snapshot = current_parse(session, row)
    active = active_parse_job(session, row)
    result["current_parse_id"] = snapshot.id if snapshot else None
    result["active_parse_job"] = (
        {"id": active.id, "status": active.status, "engine": active.payload["engine"]} if active else None
    )
    if not row.ir_fingerprint and retained_parse_state(row):
        result["ir_fingerprint"] = ir_fingerprint(row.ir)
    return result


def document_summaries(session, rows):
    if not rows:
        return []
    ids = [row.id for row in rows]
    snapshots = dict(session.execute(
        select(DocumentParseVersion.document_id, DocumentParseVersion.id)
        .join(Document, Document.id == DocumentParseVersion.document_id)
        .where(
            Document.id.in_(ids), DocumentParseVersion.tenant_id == rows[0].tenant_id,
            DocumentParseVersion.project_id == rows[0].project_id,
            DocumentParseVersion.source_revision == Document.revision,
            DocumentParseVersion.parse_revision == Document.parse_revision,
        )
    ).all())
    active = {}
    for job in session.execute(
        select(Job.id, Job.status, Job.payload["engine"].as_string().label("engine"),
               Job.payload["document_id"].as_string().label("document_id"))
        .where(
            Job.tenant_id == rows[0].tenant_id, Job.project_id == rows[0].project_id,
            Job.kind == "document.parse", Job.status.in_(ACTIVE_PARSE_STATUSES),
            Job.payload["document_id"].as_string().in_(ids),
        ).order_by(Job.created_at, Job.id)
    ):
        active.setdefault(job.document_id, {"id": job.id, "status": job.status, "engine": job.engine})
    # Only old records without a stored fingerprint need their IR inspected.
    legacy_ids = [row.id for row in rows if not row.ir_fingerprint]
    if legacy_ids:
        session.scalars(select(Document).options(load_only(Document.id, Document.ir)).where(
            Document.id.in_(legacy_ids), Document.tenant_id == rows[0].tenant_id,
        )).all()
    result = []
    for row in rows:
        item = row_dict(row, exclude=("storage_key", "ir"))
        item.update(current_parse_id=snapshots.get(row.id), active_parse_job=active.get(row.id))
        if not row.ir_fingerprint and retained_parse_state(row):
            from ontology.search.content.units import ir_fingerprint

            item["ir_fingerprint"] = ir_fingerprint(row.ir)
        result.append(item)
    return result


@router.post("/documents/upload", status_code=201)
async def upload(
    request: Request,
    project_id: str = Form(...),
    engine: str = Form("auto"),
    file: UploadFile = File(...),
    user=Depends(authenticated),
):
    if engine not in {"auto", "basic", "docling"}:
        raise DomainError(422, "invalid_parser", "未知解析器")
    db, settings = request.app.state.db, request.app.state.settings
    with db.transaction(user.tenant_id) as session:
        access(session, user, project_id, "documents:write")
    filename = Path((file.filename or "document").replace("\\", "/")).name[:250]
    from ontology.documents import DocumentError, ParseLimits
    from ontology.documents.parser import detect_format_path

    doc_id = uid()
    folder = settings.data_dir / "assets" / user.tenant_id / doc_id
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / "upload.part"
    digest, size = hashlib.sha256(), 0
    try:
        with path.open("xb") as output:
            while chunk := await file.read(1024 * 1024):
                size += len(chunk)
                if size > settings.max_upload_bytes:
                    raise DomainError(413, "upload_size", "文件超过大小限制")
                digest.update(chunk)
                output.write(chunk)
        if not size:
            raise DomainError(413, "upload_size", "文件为空")
        file_format = detect_format_path(path, filename, ParseLimits(max_bytes=settings.max_upload_bytes))
        if engine == "auto":
            engine = "basic"
        if file_format == "feishu_docx" and engine != "basic":
            raise DomainError(422, "invalid_parser", "飞书原生快照仅支持块结构解析")
        content_hash = digest.hexdigest()
        storage_key = f"{user.tenant_id}/{doc_id}/{content_hash}"
        target = folder / content_hash
        path.rename(target)
        path = target
    except DocumentError as exc:
        path.unlink(missing_ok=True)
        raise DomainError(422, exc.code, str(exc)) from None
    except BaseException:
        path.unlink(missing_ok=True)
        raise
    try:
        with db.transaction(user.tenant_id) as session:
            access(session, user, project_id, "documents:write")
            doc = Document(
                id=doc_id,
                tenant_id=user.tenant_id,
                project_id=project_id,
                filename=filename,
                content_hash=content_hash,
                storage_key=storage_key,
                size=size,
                uploader_id=user.id,
            )
            session.add(doc)
            session.flush()
            job = Job(
                tenant_id=user.tenant_id,
                project_id=project_id,
                kind="document.parse",
                owner_id=user.id,
                payload=parse_payload(doc, engine, file_format),
            )
            session.add(job)
            session.flush()
            audit(
                session,
                user,
                project_id,
                "document.uploaded",
                doc.id,
                {"size": doc.size, "content_hash": content_hash},
            )
            return {"document": row_dict(doc, exclude=("storage_key",)), "job": row_dict(job)}
    except Exception:
        path.unlink(missing_ok=True)
        raise


@router.get("/documents")
def documents(
    request: Request,
    project_id: str,
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    user=Depends(authenticated),
):
    with request.app.state.db.transaction(user.tenant_id) as session:
        ctx = content_context(session, request, user, project_id)
        conditions = [
            Document.tenant_id == user.tenant_id,
            Document.project_id == project_id,
            document_condition(ctx),
        ]
        rows = session.scalars(
            select(Document)
            .options(defer(Document.ir), defer(Document.storage_key))
            .where(*conditions)
            .order_by(Document.created_at.desc(), Document.id.desc())
            .offset(offset)
            .limit(limit)
        ).all()
        return {
            "items": document_summaries(session, rows),
            "total": session.scalar(select(func.count()).select_from(Document).where(*conditions)),
        }


@router.get("/documents/{document_id}")
def document(document_id: str, request: Request, user=Depends(authenticated)):
    with request.app.state.db.transaction(user.tenant_id) as session:
        row = scoped_record(session, Document, document_id, user)
        ctx = content_context(session, request, user, row.project_id)
        require_document(session, ctx, row.id)
        return document_view(session, row)


@router.get("/documents/{document_id}/download")
def download(
    document_id: str,
    request: Request,
    expected_revision: int | None = Query(default=None, ge=1),
    expected_hash: str | None = Query(default=None, pattern="^[a-f0-9]{64}$"),
    user=Depends(authenticated),
):
    with request.app.state.db.transaction(user.tenant_id) as session:
        row = scoped_record(session, Document, document_id, user)
        ctx = content_context(session, request, user, row.project_id)
        require_document(session, ctx, row.id, action="export")
        session.refresh(row)
        if (expected_revision is None) != (expected_hash is None):
            raise DomainError(422, "download_baseline", "下载命中原件需同时提供版本和内容哈希")
        if expected_revision is not None and (row.revision, row.content_hash) != (
            expected_revision,
            expected_hash,
        ):
            conflict("命中原件的版本已改变，请重新检索")
        path = request.app.state.settings.data_dir / "assets" / row.storage_key
        if not path.is_file():
            raise DomainError(409, "asset_missing", "原件缺失，请联系管理员恢复")
        if expected_revision is not None or session.scalar(
            select(DocumentSource.id).where(DocumentSource.document_id == row.id)
        ):
            # Read once and send exactly the verified bytes; FileResponse would
            # reopen the path after leaving the authorization transaction.
            with path.open("rb") as handle:
                content = handle.read(row.size + 1)
            if len(content) != row.size or hashlib.sha256(content).hexdigest() != row.content_hash:
                conflict("原件内容校验失败，请联系管理员恢复")
            return Response(
                content,
                media_type="application/octet-stream",
                headers={
                    "Content-Disposition": f"attachment; filename*=utf-8''{quote(row.filename)}",
                    "X-Content-Type-Options": "nosniff",
                },
            )
        return FileResponse(
            path,
            filename=row.filename,
            media_type="application/octet-stream",
            headers={"X-Content-Type-Options": "nosniff"},
        )


def page_interpretation(session, document, native_parse_id):
    if native_parse_id is None:
        return document.ir
    if native_parse_id == "native":
        return None
    snapshot = session.scalar(
        select(DocumentParseVersion).where(
            DocumentParseVersion.id == native_parse_id,
            DocumentParseVersion.tenant_id == document.tenant_id,
            DocumentParseVersion.project_id == document.project_id,
            DocumentParseVersion.document_id == document.id,
        )
    )
    if not snapshot:
        missing()
    if (snapshot.source_revision, snapshot.content_hash) != (document.revision, document.content_hash):
        conflict("历史解析对应原件已改变，未替换为当前版本")
    return snapshot.ir


@router.get("/documents/{document_id}/pages")
def document_pages(
    document_id: str,
    request: Request,
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
    native_parse_id: str | None = None,
    user=Depends(authenticated),
):
    from ontology.documents import DocumentError, ParseLimits
    from ontology.documents.pages import file_digest, iter_page_inputs, page_count, page_structure
    from ontology.documents.parser import detect_format_path

    with request.app.state.db.transaction(user.tenant_id) as session:
        row = scoped_record(session, Document, document_id, user)
        ctx = content_context(session, request, user, row.project_id)
        require_document(session, ctx, row.id)
        path = request.app.state.settings.data_dir / "assets" / row.storage_key
        if not path.is_file():
            raise DomainError(409, "asset_missing", "原件缺失，请联系管理员恢复")
        if file_digest(path) != row.content_hash:
            conflict("原件内容已改变，请核对来源版本")
        kwargs = {
            "source_id": row.id, "source_revision": str(row.revision),
            "ir": page_interpretation(session, row, native_parse_id),
            "limits": ParseLimits(max_bytes=request.app.state.settings.max_upload_bytes),
        }
        try:
            if detect_format_path(path, row.filename, kwargs["limits"]) not in {"pdf", "png", "jpeg"}:
                kwargs["ir"] = page_structure(path, row.filename, **kwargs)
            total = page_count(path, row.filename, **kwargs)
            numbers = list(range(offset + 1, min(offset + limit, total) + 1))
            items = []
            for page in iter_page_inputs(
                path, row.filename, page_numbers=numbers, include_images=False,
                verified_hash=row.content_hash, **kwargs
            ):
                page.pop("image_data_url", None)
                page.pop("image_data_urls", None)
                base = f"/api/documents/{row.id}/pages/{page['page_number']}/image"
                locator = {"native_parse_id": native_parse_id} if native_parse_id is not None else {}
                page["image_url"] = (
                    base + ("?" + urlencode(locator) if locator else "")
                    if page["has_image"]
                    else None
                )
                page["image_urls"] = [
                    base + "?" + urlencode({**locator, "image_index": index})
                    for index in range(page.get("image_count", 0))
                ]
                items.append(page)
        except DocumentError as exc:
            raise DomainError(422, exc.code, str(exc)) from None
        return {
            "items": items,
            "total": total,
            "document_id": row.id,
            "revision": row.revision,
            "content_hash": row.content_hash,
            "current_parse_id": (snapshot.id if (snapshot := current_parse(session, row)) else None),
            "native_parse_id": native_parse_id,
        }


@router.get("/documents/{document_id}/pages/{page_number}/image")
def document_page_image(
    document_id: str,
    page_number: int,
    request: Request,
    image_index: int = Query(0, ge=0),
    native_parse_id: str | None = None,
    user=Depends(authenticated),
):
    from ontology.documents import DocumentError, ParseLimits
    from ontology.documents.pages import file_digest, page_image

    with request.app.state.db.transaction(user.tenant_id) as session:
        row = scoped_record(session, Document, document_id, user)
        ctx = content_context(session, request, user, row.project_id)
        require_document(session, ctx, row.id)
        path = request.app.state.settings.data_dir / "assets" / row.storage_key
        if not path.is_file():
            raise DomainError(409, "asset_missing", "原件缺失，请联系管理员恢复")
        if file_digest(path) != row.content_hash:
            conflict("原件内容已改变，请核对来源版本")
        try:
            png = page_image(
                path,
                row.filename,
                source_id=row.id,
                source_revision=str(row.revision),
                page_number=page_number,
                ir=page_interpretation(session, row, native_parse_id),
                image_index=image_index,
                verified_hash=row.content_hash,
                limits=ParseLimits(max_bytes=request.app.state.settings.max_upload_bytes),
            )
        except DocumentError as exc:
            raise DomainError(422, exc.code, str(exc)) from None
        return Response(
            png,
            media_type="image/png",
            headers={"Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff"},
        )
