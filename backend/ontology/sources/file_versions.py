"""Same-source immutable uploads with an explicit business key and update mode."""

import hashlib
from pathlib import Path
from types import SimpleNamespace
from typing import Literal

from fastapi import APIRouter, Depends, File, Form, Query, Request, UploadFile
from fastapi.responses import FileResponse
from pydantic import Field
from sqlalchemy import func, select

from ontology.core.domain import row_dict, scoped_record
from ontology.core.errors import DomainError, conflict
from ontology.core.models import uid
from ontology.core.schemas import StrictModel
from ontology.identity.security import access, audit, authenticated
from ontology.jobs.models import Job
from ontology.objects.contributions.models import SourceContribution
from ontology.sources.ingestion import schema_fingerprint
from ontology.sources.models import (
    Source,
    SourceFileVersion,
    SourceIdentity,
    SourceRecordRevision,
    SourceSnapshot,
)
from ontology.sources.service import source_view
from ontology.sources.snapshot_api import enqueue_snapshot
from ontology.sources.tabular import MAX_BYTES, MAX_EXPANDED_BYTES, TabularError, read_tables
from ontology.sources.tabular_connector import UploadedTablesConnector, bundle_path, serialize_tables

router = APIRouter()


def read_upload(file, settings, encoding, delimiter):
    filename = Path((file.filename or "data.csv").replace("\\", "/")).name[:250]
    content = file.file.read(min(MAX_BYTES, settings.max_upload_bytes) + 1)
    if not content or len(content) > min(MAX_BYTES, settings.max_upload_bytes):
        raise DomainError(413, "upload_size", "表格不能为空，且不能超过 10 MiB 或服务端配置限制")
    try:
        tables = read_tables(content, filename, encoding=encoding, delimiter=delimiter)
    except TabularError as exc:
        raise DomainError(422, "invalid_table", str(exc)) from None
    normalized = serialize_tables(tables)
    if len(normalized) > MAX_EXPANDED_BYTES:
        raise DomainError(422, "table_budget", "规范化数据超出处理预算，请拆分文件")
    return filename, content, tables, normalized


def require_tabular(source):
    if source.kind != "tabular":
        raise DomainError(422, "not_uploaded_table", "此操作仅用于上传表格")


def keyed_rows(table, fields):
    allowed = {f"c{i + 1}" for i in range(len(table.columns))}
    if not fields or len(set(fields)) != len(fields) or set(fields) - allowed:
        raise DomainError(422, "invalid_business_key", "请选择不重复的业务字段；行号不能作为跨版本身份")
    result = {}
    for row in table.rows:
        values = {f"c{i + 1}": v for i, v in enumerate(row)}
        key = tuple(values[field] for field in fields)
        if any(value is None or value == "" for value in key) or key in result:
            raise DomainError(422, "non_unique_business_key", "业务键存在空值或重复，请先修正文件")
        result[key] = values
    return result


def withdrawn_records(session, source, tables, mode):
    """Count live source records, not objects or only the previous upload's rows."""
    counts = {f"sheet_{i + 1}": 0 for i in range(len(tables))}
    if mode != "replace":
        return counts
    keys = source.config["business_keys"]
    incoming = {resource: set(keyed_rows(table, keys[resource])) for resource, table in zip(counts, tables)}
    absent = {resource: set() for resource in counts}
    rows = session.execute(
        select(SourceIdentity.resource_id, SourceRecordRevision.source_key)
        .join(SourceContribution, SourceContribution.identity_id == SourceIdentity.id)
        .join(SourceRecordRevision, SourceRecordRevision.id == SourceIdentity.last_record_id)
        .where(
            SourceIdentity.tenant_id == source.tenant_id,
            SourceIdentity.project_id == source.project_id,
            SourceIdentity.source_id == source.id,
            SourceIdentity.resource_id.in_(counts),
            SourceContribution.active.is_(True),
        )
    )
    for resource, key in rows:
        identity = tuple(key[field] for field in keys[resource])
        if identity not in incoming[resource]:
            absent[resource].add(identity)
    return {resource: len(values) for resource, values in absent.items()}


class FileIdentity(StrictModel):
    expected_revision: int = Field(ge=1)
    business_keys: dict[str, list[str]]


@router.post("/sources/{source_id}/file-identity")
def configure_identity(source_id: str, data: FileIdentity, request: Request, user=Depends(authenticated)):
    with request.app.state.db.transaction(user.tenant_id, write=True) as session:
        source = scoped_record(session, Source, source_id, user)
        access(session, user, source.project_id, "sources:write")
        session.refresh(source)
        require_tabular(source)
        if source.revision != data.expected_revision:
            conflict("来源配置已改变")
        if session.scalar(select(SourceIdentity.id).where(SourceIdentity.source_id == source.id).limit(1)):
            conflict("已投影来源不能改写身份。请保留旧来源，以新来源和已发布唯一键显式绑定已有对象")
        connector = UploadedTablesConnector(source, request.app.state.settings)
        resources = {f"sheet_{i + 1}" for i in range(len(connector.tables))}
        if set(data.business_keys) != resources:
            raise DomainError(422, "missing_business_key", "每张工作表均须指定业务键")
        for i, table in enumerate(connector.tables):
            keyed_rows(table, data.business_keys[f"sheet_{i + 1}"])
        source.config = {**source.config, "business_keys": data.business_keys}
        connector.config = source.config
        source.resources = [r.model_dump(mode="json") for r in connector.discover()]
        source.resources = [{**r, "schema_fingerprint": schema_fingerprint(r)} for r in source.resources]
        source.revision += 1
        audit(
            session,
            user,
            source.project_id,
            "source.business_key.configured",
            source.id,
            {"fields": data.business_keys},
        )
        return source_view(source)


@router.get("/sources/{source_id}/versions")
def versions(
    source_id: str,
    request: Request,
    before: int | None = Query(None, ge=1),
    limit: int = Query(20, ge=1, le=100),
    user=Depends(authenticated),
):
    with request.app.state.db.transaction(user.tenant_id) as session:
        source = scoped_record(session, Source, source_id, user)
        access(session, user, source.project_id, "sources:write")
        scope = (SourceFileVersion.tenant_id == user.tenant_id, SourceFileVersion.source_id == source_id)
        total, original_bytes = session.execute(
            select(
                func.count(), func.coalesce(func.sum(SourceFileVersion.config["size"].as_integer()), 0)
            ).where(*scope)
        ).one()
        rows = session.scalars(
            select(SourceFileVersion)
            .where(*scope, *([SourceFileVersion.number < before] if before is not None else []))
            .order_by(SourceFileVersion.number.desc())
            .limit(limit + 1)
        ).all()
        page = rows[:limit]
        return {
            "items": [row_dict(r) for r in page],
            "total": total,
            "original_bytes": original_bytes,
            "next_before": page[-1].number if len(rows) > limit else None,
        }


@router.post("/sources/{source_id}/versions", status_code=201)
def preview_version(
    source_id: str,
    request: Request,
    expected_revision: int = Form(...),
    mode: Literal["upsert", "replace"] = Form("upsert"),
    encoding: str = Form("utf-8-sig"),
    delimiter: str = Form(","),
    file: UploadFile = File(...),
    user=Depends(authenticated),
):
    db, settings = request.app.state.db, request.app.state.settings
    with db.transaction(user.tenant_id) as session:
        source = scoped_record(session, Source, source_id, user)
        access(session, user, source.project_id, "sources:write")
        require_tabular(source)
    filename, content, tables, normalized = read_upload(file, settings, encoding, delimiter)
    directory = None
    written = []
    commit_started = False
    try:
        with db.transaction(user.tenant_id, write=True) as session:
            source = scoped_record(session, Source, source_id, user)
            access(session, user, source.project_id, "sources:write")
            session.refresh(source)
            if source.revision != expected_revision:
                conflict("来源配置已改变，请重新预览")
            keys = source.config.get("business_keys")
            if not keys:
                conflict("更新前须先配置明确业务键，不能依赖行号覆盖对象")
            previous = UploadedTablesConnector(source, settings).tables
            if len(previous) != len(tables) or any(
                list(a.columns) != list(b.columns) or (len(tables) > 1 and a.name != b.name)
                for a, b in zip(previous, tables)
            ):
                conflict("工作表或字段结构已改变，请作为新来源审核映射；原来源与证据保持不变")
            preview = {}
            withdrawn = withdrawn_records(session, source, tables, mode)
            for i, (old, new) in enumerate(zip(previous, tables)):
                resource = f"sheet_{i + 1}"
                left, right = keyed_rows(old, keys[resource]), keyed_rows(new, keys[resource])
                common = left.keys() & right.keys()
                preview[resource] = {
                    "added": len(right.keys() - left.keys()),
                    "updated": sum(left[k] != right[k] for k in common),
                    "unchanged": sum(left[k] == right[k] for k in common),
                    "absent": len(left.keys() - right.keys()),
                    "withdrawn": withdrawn[resource],
                }
            version_id = uid()
            config = {
                **source.config,
                "filename": filename,
                "content_sha256": hashlib.sha256(content).hexdigest(),
                "tables_sha256": hashlib.sha256(normalized).hexdigest(),
                "size": len(content),
                "encoding": encoding,
                "delimiter": delimiter,
                "bundle_id": version_id,
                "file_mode": mode,
                "frozen_fields": {r["resource_id"]: r["fields"] for r in source.resources},
            }
            holder = SimpleNamespace(
                id=source.id, tenant_id=source.tenant_id, project_id=source.project_id, config=config
            )
            candidate = bundle_path(settings, holder)
            candidate.mkdir(parents=True, exist_ok=False)
            directory = candidate
            for leaf, data in (("original", content), ("tables.json", normalized)):
                target = directory / leaf
                with target.open("xb") as stream:
                    written.append(target)
                    stream.write(data)
            row = SourceFileVersion(
                id=version_id,
                tenant_id=user.tenant_id,
                project_id=source.project_id,
                source_id=source.id,
                number=(
                    session.scalar(
                        select(func.max(SourceFileVersion.number)).where(
                            SourceFileVersion.source_id == source.id
                        )
                    )
                    or 1
                )
                + 1,
                base_revision=source.revision,
                config=config,
                resources=source.resources,
                preview=preview,
            )
            session.add(row)
            session.flush()
            result = row_dict(row)
            commit_started = True
            return result
    except Exception:
        # A lost commit response can still mean the row is durable. Preserve its
        # files for reconciliation instead of deleting possibly committed evidence.
        if not commit_started:
            for target in written:
                target.unlink(missing_ok=True)
            if directory is not None:
                directory.rmdir()
        raise


class ApplyVersion(StrictModel):
    expected_revision: int = Field(ge=1)


@router.post("/sources/{source_id}/versions/{version_id}/apply")
def apply_version(
    source_id: str, version_id: str, data: ApplyVersion, request: Request, user=Depends(authenticated)
):
    with request.app.state.db.transaction(user.tenant_id, write=True) as session:
        source = scoped_record(session, Source, source_id, user)
        ctx = access(session, user, source.project_id, "sources:write")
        session.refresh(source)
        version = scoped_record(session, SourceFileVersion, version_id, user)
        if version.source_id != source.id:
            conflict("文件版本不属于此来源")
        if version.applied_revision is not None:
            return {"source": source_view(source), "version": row_dict(version)}
        if source.revision != data.expected_revision or source.revision != version.base_revision:
            conflict("来源配置或文件版本已改变，请重新预览")
        pending = session.scalar(
            select(Job.id)
            .join(SourceSnapshot, SourceSnapshot.id == Job.payload["snapshot_id"].as_string())
            .where(
                Job.tenant_id == source.tenant_id,
                Job.project_id == source.project_id,
                Job.kind == "source.snapshot",
                Job.status.in_(["pending", "running", "retry_wait"]),
                SourceSnapshot.source_id == source.id,
            )
            .limit(1)
        )
        if pending:
            raise DomainError(
                409, "file_sync_pending", "此来源仍有未完成的同步任务，请等待完成或明确取消后再应用新版本",
                {"job_id": pending},
            )
        connector = UploadedTablesConnector(
            SimpleNamespace(
                id=source.id, tenant_id=source.tenant_id, project_id=source.project_id, config=version.config
            ),
            request.app.state.settings,
        )
        if version.config["file_mode"] == "replace":
            withdrawn = withdrawn_records(session, source, connector.tables, "replace")
            if any(
                version.preview.get(resource, {}).get("withdrawn") != count
                for resource, count in withdrawn.items()
            ):
                conflict("来源贡献已改变，请重新预览并核对本次撤回的来源记录")
        source.config, source.resources = dict(version.config), list(version.resources)
        source.revision += 1
        version.applied_revision = source.revision
        results = [enqueue_snapshot(session, ctx, source, r) for r in source.resources]
        version.jobs = [result["job"]["id"] for result in results]
        audit(
            session,
            user,
            source.project_id,
            "source.file_version.applied",
            version.id,
            {"mode": version.config["file_mode"]},
        )
        return {"source": source_view(source), "version": row_dict(version)}


@router.get("/sources/{source_id}/versions/{version_id}/original")
def original_version(source_id: str, version_id: str, request: Request, user=Depends(authenticated)):
    with request.app.state.db.transaction(user.tenant_id) as session:
        source = scoped_record(session, Source, source_id, user)
        access(session, user, source.project_id, "sources:write")
        version = scoped_record(session, SourceFileVersion, version_id, user)
        if version.source_id != source_id:
            conflict("文件版本不属于此来源")
        holder = SimpleNamespace(
            id=source.id, tenant_id=source.tenant_id, project_id=source.project_id, config=version.config
        )
        path = bundle_path(request.app.state.settings, holder) / "original"
        try:
            valid = hashlib.sha256(path.read_bytes()).hexdigest() == version.config["content_sha256"]
        except OSError:
            valid = False
        if not valid:
            conflict("原文件不存在或完整性校验未通过")
        return FileResponse(path, filename=version.config["filename"], media_type="application/octet-stream")
