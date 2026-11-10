import hashlib

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from fastapi.responses import FileResponse
from pydantic import Field

from ontology.core.domain import active_definition, scoped_record
from ontology.core.errors import DomainError, conflict
from ontology.core.models import uid
from ontology.core.schemas import StrictModel
from ontology.identity.security import access, audit, authenticated
from ontology.sources.ingestion import schema_fingerprint
from ontology.sources.models import Source, SourceFileVersion
from ontology.sources.proposals import propose
from ontology.sources.service import require_source_read_identity, source_view
from ontology.sources.tabular import MAX_BYTES
from ontology.sources.tabular_connector import UploadedTablesConnector, bundle_path

router = APIRouter()


@router.post("/sources/upload", status_code=201)
def upload_tables(
    request: Request,
    project_id: str = Form(...),
    name: str = Form(""),
    encoding: str = Form("utf-8-sig"),
    delimiter: str = Form(","),
    file: UploadFile = File(...),
    user=Depends(authenticated),
):
    db, settings = request.app.state.db, request.app.state.settings
    with db.transaction(user.tenant_id) as session:
        access(session, user, project_id, "sources:write")
    from ontology.sources.file_versions import read_upload

    filename, content, tables, normalized = read_upload(file, settings, encoding, delimiter)
    name = name.strip() or filename[:200]
    if len(name) > 200:
        raise DomainError(422, "invalid_name", "数据源名称最多 200 个字符")
    source = Source(
        id=uid(),
        tenant_id=user.tenant_id,
        project_id=project_id,
        name=name,
        kind="tabular",
        secret="",
        status="discovered",
        config={
            "filename": filename,
            "content_sha256": hashlib.sha256(content).hexdigest(),
            "tables_sha256": hashlib.sha256(normalized).hexdigest(),
            "size": len(content),
            "encoding": encoding,
            "delimiter": delimiter,
            "version": 1,
        },
    )
    path = bundle_path(settings, source)
    path.mkdir(parents=True, exist_ok=False)
    written = []
    commit_started = False
    try:
        for leaf, data in (("original", content), ("tables.json", normalized)):
            target = path / leaf
            with target.open("xb") as stream:
                written.append(target)
                stream.write(data)
        connector = UploadedTablesConnector(source, settings)
        source.resources = [r.model_dump(mode="json") for r in connector.discover()]
        for resource in source.resources:
            resource["schema_fingerprint"] = schema_fingerprint(resource)
        with db.transaction(user.tenant_id) as session:
            access(session, user, project_id, "sources:write")
            session.add(source)
            session.flush()
            session.add(
                SourceFileVersion(
                    tenant_id=source.tenant_id,
                    project_id=source.project_id,
                    source_id=source.id,
                    number=1,
                    base_revision=1,
                    applied_revision=1,
                    config=dict(source.config),
                    resources=list(source.resources),
                )
            )
            audit(
                session,
                user,
                project_id,
                "source.uploaded",
                source.id,
                {"content_sha256": source.config["content_sha256"], "tables": len(tables)},
            )
            result = source_view(source)
            commit_started = True
        return result
    except Exception:
        # Commit acknowledgement can fail after the database stored the source.
        if not commit_started:
            for target in written:
                target.unlink(missing_ok=True)
            path.rmdir()
        raise


@router.get("/sources/{source_id}/original")
def original_upload(source_id: str, request: Request, user=Depends(authenticated)):
    with request.app.state.db.transaction(user.tenant_id) as session:
        source = scoped_record(session, Source, source_id, user)
        access(session, user, source.project_id, "sources:write")
        if source.kind != "tabular":
            raise DomainError(404, "not_found", "该来源没有上传原文件")
        path = bundle_path(request.app.state.settings, source) / "original"
        try:
            with path.open("rb") as stream:
                data = stream.read(MAX_BYTES + 1)
            if len(data) > MAX_BYTES or hashlib.sha256(data).hexdigest() != source.config["content_sha256"]:
                raise ValueError()
        except (OSError, ValueError):
            raise DomainError(409, "original_unavailable", "原文件不存在或完整性校验未通过") from None
        return FileResponse(
            path,
            filename=source.config["filename"],
            media_type="application/octet-stream",
            headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"},
        )


class ProposalInput(StrictModel):
    expected_source_revision: int = Field(ge=1)
    resource_ids: list[str] = Field(min_length=1, max_length=32)


@router.post("/sources/{source_id}/model-proposal")
def model_proposal(source_id: str, data: ProposalInput, request: Request, user=Depends(authenticated)):
    with request.app.state.db.transaction(user.tenant_id) as session:
        source = scoped_record(session, Source, source_id, user)
        ctx = access(session, user, source.project_id, "sources:write")
        access(session, user, source.project_id, "ontology:write")
        require_source_read_identity(source)
        if source.revision != data.expected_source_revision:
            conflict("来源配置已改变，请重新发现资源")
        return {
            **propose(source, data.resource_ids, active_definition(session, ctx.project)),
            "baseline_release_id": ctx.project.active_release_id,
            "source_revision": source.revision,
        }
