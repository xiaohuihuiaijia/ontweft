from fastapi import APIRouter, Depends, Query, Request
from pydantic import Field
from sqlalchemy import func, select

from ontology.core.domain import exact_json_text, row_dict, scoped_record
from ontology.core.errors import DomainError, conflict
from ontology.core.schemas import StrictModel
from ontology.identity.security import access, audit, authenticated
from ontology.jobs.models import Job
from ontology.sources.ingestion import schema_fingerprint
from ontology.sources.models import Source, SourceRecordRevision, SourceSnapshot

router = APIRouter()


class SnapshotInput(StrictModel):
    expected_source_revision: int = Field(ge=1)
    resource_id: str = Field(min_length=1, max_length=500)


@router.post("/sources/{source_id}/snapshots", status_code=201)
def create_snapshot(source_id: str, data: SnapshotInput, request: Request, user=Depends(authenticated)):
    with request.app.state.db.transaction(user.tenant_id) as session:
        source = scoped_record(session, Source, source_id, user)
        ctx = access(session, user, source.project_id, "sources:write")
        if source.revision != data.expected_source_revision:
            conflict("来源配置已改变")
        if source.kind == "feishu":
            raise DomainError(
                422, "employee_authorization_required", "飞书批量持久接入须先配置当前员工授权服务"
            )
        resource = next((r for r in source.resources if r["resource_id"] == data.resource_id), None)
        if not resource:
            raise DomainError(422, "undiscovered_resource", "请先发现并选择有效资源")
        generation = (
            session.scalar(
                select(func.max(SourceSnapshot.generation)).where(
                    SourceSnapshot.source_id == source.id, SourceSnapshot.resource_id == data.resource_id
                )
            )
            or 0
        ) + 1
        snapshot = SourceSnapshot(
            tenant_id=user.tenant_id,
            project_id=source.project_id,
            source_id=source.id,
            source_revision=source.revision,
            resource_id=data.resource_id,
            schema_fingerprint=schema_fingerprint(resource),
            release_id=ctx.project.active_release_id,
            generation=generation,
        )
        session.add(snapshot)
        session.flush()
        job = Job(
            tenant_id=user.tenant_id,
            project_id=source.project_id,
            kind="source.snapshot",
            owner_id=user.id,
            payload={"snapshot_id": snapshot.id, "source_id": source.id},
        )
        session.add(job)
        session.flush()
        audit(session, user, source.project_id, "source.snapshot.requested", snapshot.id)
        return {"snapshot": row_dict(snapshot), "job": row_dict(job)}


@router.get("/sources/{source_id}/snapshots")
def snapshots(
    source_id: str,
    request: Request,
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    user=Depends(authenticated),
):
    with request.app.state.db.transaction(user.tenant_id) as session:
        source = scoped_record(session, Source, source_id, user)
        access(session, user, source.project_id, "sources:write")
        conditions = (SourceSnapshot.source_id == source_id, SourceSnapshot.tenant_id == user.tenant_id)
        rows = session.scalars(
            select(SourceSnapshot)
            .where(*conditions)
            .order_by(SourceSnapshot.created_at.desc())
            .offset(offset)
            .limit(limit)
        ).all()
        return {
            "items": [row_dict(row) for row in rows],
            "total": session.scalar(select(func.count()).select_from(SourceSnapshot).where(*conditions)),
        }


@router.get("/snapshots/{snapshot_id}/records")
def records(
    snapshot_id: str,
    request: Request,
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    user=Depends(authenticated),
):
    with request.app.state.db.transaction(user.tenant_id) as session:
        snapshot = scoped_record(session, SourceSnapshot, snapshot_id, user)
        access(session, user, snapshot.project_id, "sources:write")
        conditions = (
            SourceRecordRevision.snapshot_id == snapshot.id,
            SourceRecordRevision.tenant_id == user.tenant_id,
            SourceRecordRevision.attempt == snapshot.attempt,
        )
        rows = session.scalars(
            select(SourceRecordRevision)
            .where(*conditions)
            .order_by(SourceRecordRevision.id)
            .offset(offset)
            .limit(limit)
        ).all()
        return {
            "snapshot": row_dict(snapshot),
            "items": [
                {
                    **row_dict(row),
                    "source_key_json": exact_json_text(row.source_key),
                    "values_json": exact_json_text(row.values),
                }
                for row in rows
            ],
            "total": session.scalar(
                select(func.count()).select_from(SourceRecordRevision).where(*conditions)
            ),
        }


@router.get("/source-records/{record_id}")
def record_detail(record_id: str, request: Request, user=Depends(authenticated)):
    with request.app.state.db.transaction(user.tenant_id) as session:
        record = scoped_record(session, SourceRecordRevision, record_id, user)
        access(session, user, record.project_id, "sources:write")
        value = row_dict(record)
        return {**value, "record_json": exact_json_text(value)}
