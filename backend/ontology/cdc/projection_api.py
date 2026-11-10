"""Small, explicit configuration surface for the existing durable CDC inbox."""

from fastapi import APIRouter, Depends, Request
from pydantic import Field
from sqlalchemy import select

from ontology.cdc.models import CdcStream
from ontology.cdc.projection_models import CdcProjectionBinding, CdcProjectionState, CdcProjectionWindow
from ontology.core.domain import active_definition, row_dict, scoped_record
from ontology.core.errors import DomainError, conflict
from ontology.core.schemas import StrictModel
from ontology.identity.security import access, audit, authenticated
from ontology.jobs.models import Job
from ontology.sources.ingestion import fingerprint, mapping_errors
from ontology.sources.models import Source

router = APIRouter()


class ProjectionInput(StrictModel):
    expected_release_id: str
    expected_source_revision: int = Field(ge=1)
    resource_ids: list[str] = Field(min_length=1, max_length=1000)


class ProjectInput(StrictModel):
    expected_binding_id: str


def configuration(session, ctx, stream):
    source = scoped_record(session, Source, stream.source_id, ctx.user)
    definition = active_definition(session, ctx.project)
    mappings = [m for m in definition.get("mappings", []) if m["source_id"] == source.id]
    resources = [
        {
            "resource_id": r["resource_id"],
            "label": r.get("name") or r["resource_id"],
            "mapping_keys": [m["key"] for m in mappings if m["resource_id"] == r["resource_id"]],
            "covered": any(m["resource_id"] == r["resource_id"] for m in mappings),
        }
        for r in source.resources
    ]
    return source, definition, resources


def projection_view(session, ctx, stream):
    from ontology.search.objects.index import cdc_index_status

    source, _definition, resources = configuration(session, ctx, stream)
    binding = session.scalar(select(CdcProjectionBinding).where(CdcProjectionBinding.stream_id == stream.id))
    state = session.scalar(select(CdcProjectionState).where(CdcProjectionState.stream_id == stream.id))
    job = session.get(Job, state.last_job_id) if state and state.last_job_id else None
    window = (
        session.get(CdcProjectionWindow, state.baseline_window_id)
        if state and state.baseline_window_id
        else None
    )
    current = {
        "release_id": ctx.project.active_release_id,
        "version": ctx.project.version,
        "source_revision": source.revision,
    }
    status = state.status if state else "not_configured"
    reason, message = (
        (state.reason_code, state.message)
        if state
        else ("not_configured", "选择已发布映射覆盖的完整采集范围")
    )
    if binding and (
        binding.release_id != ctx.project.active_release_id
        or binding.source_revision != source.revision
        or binding.generation != stream.generation
    ):
        status, reason, message = (
            "blocked",
            "binding_changed",
            "模型或来源基线已改变，需要审核迁移后重新建立绑定",
        )
    elif binding and stream.state != "active":
        status, reason, message = "blocked", "stream_paused", "采集通道已暂停"
    elif job and job.status in {"pending", "running"}:
        status, reason, message = (
            "running",
            "job_pending",
            "后台任务已排队或正在执行，提交水位仍以事务提交结果为准",
        )
    return {
        "binding": None
        if not binding
        else {
            "id": binding.id,
            "release_id": binding.release_id,
            "source_revision": binding.source_revision,
            "generation": binding.generation,
            "resource_ids": [r["resource_id"] for r in binding.resources],
            "created_at": binding.created_at,
        },
        "current": current,
        "resources": resources,
        "status": status,
        "reason_code": reason,
        "message": message,
        "received_sequence": stream.received_sequence,
        "projected_sequence": stream.projected_sequence,
        "indexed_sequence": stream.indexed_sequence,
        "indexing_status": "not_configured",
        "fulltext": cdc_index_status(session, ctx.project, stream),
        "baseline": None
        if not window
        else {
            "window_id": window.id,
            "end_sequence": window.end_sequence,
            "proof_kind": window.proof["kind"],
        },
        "last_job": None
        if not job
        else {"id": job.id, "status": job.status, "error": job.error, "result": job.result},
        "can_project": bool(
            binding
            and status not in {"blocked", "budget_exceeded", "running"}
            and stream.projected_sequence < stream.received_sequence
        ),
        "can_configure": "ontology:write" in ctx.permissions and binding is None,
    }


@router.get("/cdc/streams/{stream_id}/projection")
def get_projection(stream_id: str, request: Request, user=Depends(authenticated)):
    with request.app.state.db.transaction(user.tenant_id) as session:
        stream = scoped_record(session, CdcStream, stream_id, user)
        ctx = access(session, user, stream.project_id, "sources:write")
        session.refresh(ctx.project)
        session.refresh(stream)
        return projection_view(session, ctx, stream)


@router.post("/cdc/streams/{stream_id}/projection")
def configure(stream_id: str, data: ProjectionInput, request: Request, user=Depends(authenticated)):
    with request.app.state.db.transaction(user.tenant_id) as session:
        stream = scoped_record(session, CdcStream, stream_id, user)
        ctx = access(session, user, stream.project_id, "sources:write")
        access(session, user, stream.project_id, "ontology:write")
        session.refresh(ctx.project)
        session.refresh(stream, with_for_update=True)
        source, definition, resources = configuration(session, ctx, stream)
        session.refresh(source)
        if (
            ctx.project.active_release_id != data.expected_release_id
            or source.revision != data.expected_source_revision
            or stream.source_revision != source.revision
        ):
            conflict("模型或来源配置已改变，请刷新后重新选择")
        if not ctx.project.active_release_id or source.kind != "postgres" or stream.state != "active":
            conflict("需要已发布模型与活动的 PostgreSQL 采集通道")
        if len(data.resource_ids) != len(set(data.resource_ids)):
            raise DomainError(422, "duplicate_resource", "采集范围不能重复")
        covered = {r["resource_id"] for r in resources if r["covered"]}
        if set(data.resource_ids) != covered or not covered:
            raise DomainError(
                422,
                "projection_scope_mismatch",
                "投影范围必须恰好覆盖该来源全部已发布映射资源，并与采集端配置一致",
            )
        errors = mapping_errors(session, ctx, definition)
        if errors:
            raise DomainError(422, "mapping_invalid", "已发布映射与来源目录不一致", errors)
        frozen = sorted(
            (r for r in source.resources if r["resource_id"] in covered), key=lambda r: r["resource_id"]
        )
        for resource in frozen:
            if (
                len(resource["resource_id"].split(".")) != 2
                or "," in resource["resource_id"]
                or not resource["primary_key"]
            ):
                raise DomainError(
                    422, "unsupported_snapshot_scope", "资源必须使用 schema.table 标识及稳定主键"
                )
            fields = [field["name"] for field in resource["fields"]]
            keys = resource["primary_key"]
            if (
                len(fields) != len(set(fields))
                or len(keys) != len(set(keys))
                or not set(keys).issubset(fields)
            ):
                raise DomainError(422, "invalid_source_identity", "主键必须由目录中存在且不重复的字段组成")
            for field in resource["fields"]:
                meta = field.get("metadata", {})
                if field["name"] in resource["primary_key"] and (
                    meta.get("udt_name") in {"citext", "bpchar"}
                    or meta.get("collation_deterministic") is False
                ):
                    raise DomainError(
                        422, "unsupported_key_semantics", "当前主键比较语义需要显式稳定身份设计"
                    )
        signature = fingerprint(
            {
                "release_id": data.expected_release_id,
                "source_revision": source.revision,
                "generation": stream.generation,
                "resources": frozen,
            }
        )
        binding = session.scalar(
            select(CdcProjectionBinding).where(CdcProjectionBinding.stream_id == stream.id)
        )
        if binding:
            if binding.binding_hash != signature:
                conflict("已有不可变投影绑定，修改必须经过审核迁移和重建基线流程")
            return projection_view(session, ctx, stream)
        binding = CdcProjectionBinding(
            tenant_id=user.tenant_id,
            project_id=stream.project_id,
            stream_id=stream.id,
            source_id=source.id,
            source_revision=source.revision,
            generation=stream.generation,
            release_id=data.expected_release_id,
            resources=frozen,
            binding_hash=signature,
            owner_id=user.id,
        )
        session.add(binding)
        session.flush()
        session.add(
            CdcProjectionState(
                tenant_id=user.tenant_id,
                project_id=stream.project_id,
                stream_id=stream.id,
                binding_id=binding.id,
            )
        )
        session.flush()
        audit(
            session,
            user,
            stream.project_id,
            "cdc.projection.configured",
            binding.id,
            {"resource_ids": data.resource_ids},
        )
        return projection_view(session, ctx, stream)


@router.post("/cdc/streams/{stream_id}/project", status_code=202)
def project(stream_id: str, data: ProjectInput, request: Request, user=Depends(authenticated)):
    from ontology.cdc.projection import enqueue_projection

    with request.app.state.db.transaction(user.tenant_id) as session:
        stream = scoped_record(session, CdcStream, stream_id, user)
        ctx = access(session, user, stream.project_id, "sources:write")
        session.refresh(ctx.project)
        session.refresh(stream, with_for_update=True)
        state = session.scalar(
            select(CdcProjectionState).where(CdcProjectionState.stream_id == stream.id).with_for_update()
        )
        if not state or state.binding_id != data.expected_binding_id:
            conflict("投影绑定不存在或已改变")
        view = projection_view(session, ctx, stream)
        if view["status"] in {"blocked", "budget_exceeded"}:
            conflict("当前投影需要先解决基线或完整窗口问题，不能盲目重试")
        if not view["can_project"] and view["status"] != "running":
            conflict("没有待投影的已接收事件")
        job = enqueue_projection(session, stream, state, user.id)
        return {"job": row_dict(job)}
