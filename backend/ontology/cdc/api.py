import json
import time
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request
from pydantic import Field
from sqlalchemy import select
from starlette.concurrency import run_in_threadpool

from ontology.cdc.models import CdcEvent, CdcStream
from ontology.cdc.projection_api import ProjectionInput, configure_binding
from ontology.cdc.projection_models import CdcProjectionState
from ontology.cdc.runtime import LEASE_SECONDS, authenticated_stream, issue_token, read_batch, receive_batch
from ontology.core.domain import row_dict, scoped_record
from ontology.core.errors import DomainError, conflict, forbidden
from ontology.core.models import uid
from ontology.core.schemas import StrictModel
from ontology.identity.security import access, audit, authenticated
from ontology.jobs.models import Job
from ontology.sources.models import Source

router = APIRouter()


class StreamInput(StrictModel):
    source_id: str
    expected_source_revision: int = Field(ge=1)


class LeaseInput(StrictModel):
    holder_id: str = Field(min_length=36, max_length=36)


class RebuildInput(ProjectionInput):
    expected_binding_id: str | None = None


def stream_view(row, session):
    status = session.scalar(select(CdcProjectionState.status).where(CdcProjectionState.stream_id == row.id))
    return {**row_dict(row, exclude=("token_hash",)), "projection_status": status or "not_configured"}


@router.post("/cdc/streams", status_code=201)
def create_stream(data: StreamInput, request: Request, user=Depends(authenticated)):
    with request.app.state.db.transaction(user.tenant_id, write=True) as session:
        source = scoped_record(session, Source, data.source_id, user)
        access(session, user, source.project_id, "sources:write")
        session.refresh(source)
        if source.revision != data.expected_source_revision:
            conflict("来源配置版本已变化")
        if source.kind != "postgres":
            raise DomainError(
                422,
                "cdc_connector_unavailable",
                "当前原生 CDC 适配仅开放 PostgreSQL，其他入口使用已有快照链路",
            )
        if session.scalar(select(CdcStream.id).where(CdcStream.source_id == source.id)):
            conflict("来源已有持久采集通道，请续接或审查重新建立基线，不能以新通道跳过历史")
        stream = CdcStream(
            id=uid(),
            tenant_id=user.tenant_id,
            project_id=source.project_id,
            source_id=source.id,
            source_revision=source.revision,
            generation=uid(),
            connector="postgresql",
            owner_id=user.id,
        )
        token = issue_token(stream)
        session.add(stream)
        session.flush()
        audit(session, user, source.project_id, "cdc.stream.created", stream.id)
        return {"stream": stream_view(stream, session), "token": token}


@router.get("/cdc/streams")
def streams(request: Request, project_id: str, user=Depends(authenticated)):
    with request.app.state.db.transaction(user.tenant_id) as session:
        access(session, user, project_id, "sources:write")
        rows = session.scalars(
            select(CdcStream)
            .where(CdcStream.project_id == project_id, CdcStream.tenant_id == user.tenant_id)
            .order_by(CdcStream.created_at.desc())
        ).all()
        return {"items": [stream_view(row, session) for row in rows]}


@router.post("/cdc/streams/{stream_id}/lease")
def lease(stream_id: str, data: LeaseInput, request: Request):
    try:
        if str(UUID(data.holder_id)) != data.holder_id:
            raise ValueError()
    except ValueError as exc:
        raise DomainError(422, "invalid_holder", "采集进程身份必须为 UUID") from exc
    with authenticated_stream(request.app.state.db, request.headers.get("authorization"), stream_id) as (
        _,
        stream,
    ):
        current = time.time()
        if stream.lease_until > current and stream.lease_holder != data.holder_id:
            conflict("另一采集端仍持有租约")
        if stream.lease_holder != data.holder_id or stream.lease_until <= current:
            stream.fencing_token += 1
        stream.lease_holder, stream.lease_until = data.holder_id, current + LEASE_SECONDS
        response = {
            "fencing_token": stream.fencing_token,
            "lease_seconds": LEASE_SECONDS,
            "received_sequence": stream.received_sequence,
            "generation": stream.generation,
        }
    return response


@router.post("/cdc/streams/{stream_id}/append")
async def append(stream_id: str, request: Request):
    batch = await read_batch(request)
    return await run_in_threadpool(
        commit_batch, request.app.state.db, request.headers.get("authorization"), stream_id, batch
    )


def commit_batch(db, authorization, stream_id, batch):
    with authenticated_stream(db, authorization, stream_id) as (session, stream):
        response = receive_batch(session, stream, batch)
    return response


@router.post("/cdc/streams/{stream_id}/rebuild", status_code=201)
def rebuild(stream_id: str, data: RebuildInput, request: Request, user=Depends(authenticated)):
    with request.app.state.db.transaction(user.tenant_id, write=True) as session:
        old = scoped_record(session, CdcStream, stream_id, user)
        ctx = access(session, user, old.project_id, "sources:write")
        if "ontology:write" not in ctx.permissions:
            forbidden()
        session.refresh(ctx.project)
        session.refresh(old, with_for_update=True)
        if old.state == "retired":
            conflict("该代已退出，请读取当前通道；不会重复建立新代")
        state = session.scalar(select(CdcProjectionState).where(CdcProjectionState.stream_id == old.id))
        if (state.binding_id if state else None) != data.expected_binding_id:
            conflict("投影绑定已改变，请刷新后重建")
        source = scoped_record(session, Source, old.source_id, user)
        old.state, old.lease_holder, old.lease_until = "retired", None, 0
        old.fencing_token += 1
        if state and state.last_job_id:
            job = session.get(Job, state.last_job_id)
            if job and job.status in {"pending", "running"}:
                job.status, job.lease_until = "cancelled", 0
        session.flush()
        stream = CdcStream(
            id=uid(),
            tenant_id=user.tenant_id,
            project_id=old.project_id,
            source_id=source.id,
            source_revision=source.revision,
            previous_stream_id=old.id,
            generation=uid(),
            connector="postgresql",
            owner_id=user.id,
        )
        token = issue_token(stream)
        session.add(stream)
        session.flush()
        configure_binding(session, ctx, stream, data)
        audit(
            session,
            user,
            old.project_id,
            "cdc.baseline.rebuild",
            stream.id,
            {"previous_stream_id": old.id, "previous_projected_sequence": old.projected_sequence},
        )
        return {"stream": stream_view(stream, session), "token": token}


@router.post("/cdc/streams/{stream_id}/{operation}")
def operate(stream_id: str, operation: str, request: Request, user=Depends(authenticated)):
    with request.app.state.db.transaction(user.tenant_id) as session:
        row = scoped_record(session, CdcStream, stream_id, user)
        access(session, user, row.project_id, "sources:write")
        session.refresh(row)
        if row.state == "retired":
            conflict("历史采集代只读，不能恢复或换令牌")
        token = None
        if operation == "pause" and row.state == "active":
            row.state = "paused"
        elif operation == "resume" and row.state == "paused":
            row.state = "active"
        elif operation == "rotate-token":
            token = issue_token(row)
        else:
            conflict("采集通道当前状态不允许该操作")
        row.fencing_token += 1
        row.lease_until, row.lease_holder = 0, None
        audit(session, user, row.project_id, f"cdc.stream.{operation}", row.id)
        response = {"stream": stream_view(row, session)}
        if token:
            response["token"] = token
        return response


@router.get("/cdc/streams/{stream_id}/events")
def events(
    stream_id: str,
    request: Request,
    after: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=200),
    user=Depends(authenticated),
):
    with request.app.state.db.transaction(user.tenant_id) as session:
        stream = scoped_record(session, CdcStream, stream_id, user)
        access(session, user, stream.project_id, "sources:write")
        rows = session.scalars(
            select(CdcEvent)
            .where(
                CdcEvent.stream_id == stream_id,
                CdcEvent.tenant_id == user.tenant_id,
                CdcEvent.sequence > after,
            )
            .order_by(CdcEvent.sequence)
            .limit(limit)
        ).all()
        return {
            "stream": stream_view(stream, session),
            "items": [
                {
                    **row_dict(row),
                    "record_json": json.dumps(
                        row.record, ensure_ascii=False, separators=(",", ":"), allow_nan=False
                    ),
                }
                for row in rows
            ],
        }
