import time
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy import func, select, update

from ontology.content_access.service import (
    content_context,
    job_condition,
    prepare_content_access,
    require_document,
)
from ontology.core.domain import row_dict, scoped_record
from ontology.core.errors import DomainError, conflict
from ontology.core.models import now
from ontology.documents.history import (
    active_parse_job,
    check_parse_baseline,
    retained_parse_state,
    validate_job_configuration,
)
from ontology.documents.storage_models import Document
from ontology.identity.models import Project
from ontology.identity.security import audit, authenticated
from ontology.jobs.models import Job
from ontology.search.content.index import enqueue_content
from ontology.sources.models import SourceSnapshot

router = APIRouter(dependencies=[Depends(prepare_content_access)])


@router.get("/jobs")
def jobs(
    request: Request,
    project_id: str,
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    user=Depends(authenticated),
):
    with request.app.state.db.transaction(user.tenant_id) as session:
        ctx = content_context(session, request, user, project_id)
        conditions = [Job.tenant_id == user.tenant_id, Job.project_id == project_id]
        conditions.append(job_condition(ctx))
        if "jobs:manage" not in ctx.permissions:
            conditions.append(Job.owner_id == user.id)
        rows = session.scalars(
            select(Job).where(*conditions).order_by(Job.created_at.desc()).offset(offset).limit(limit)
        ).all()
        return {
            "items": [row_dict(x) for x in rows],
            "total": session.scalar(select(func.count()).select_from(Job).where(*conditions)),
        }


@router.post("/jobs/{job_id}/{operation}")
def job_operation(job_id: str, operation: str, request: Request, user=Depends(authenticated)):
    with request.app.state.db.transaction(user.tenant_id, write=True) as session:
        row = scoped_record(session, Job, job_id, user)
        session.scalar(
            select(Project)
            .where(
                Project.id == row.project_id,
                Project.tenant_id == user.tenant_id,
            )
            .with_for_update()
        )
        session.refresh(row)
        ctx = content_context(session, request, user, row.project_id)
        if row.kind == "document.parse":
            require_document(session, ctx, row.payload.get("document_id"))
        if row.owner_id != user.id and "jobs:manage" not in ctx.permissions:
            raise DomainError(403, "forbidden", "只能管理自己的任务")
        if operation == "cancel" and row.status in {"pending", "running", "retry_wait"}:
            status = "cancelled"
        elif operation == "retry" and row.status in {"failed", "cancelled"}:
            from ontology.jobs.service import check_job_authority

            check_job_authority(session, row)
            if row.kind == "document.parse":
                doc = session.get(Document, row.payload["document_id"])
                check_parse_baseline(doc, row)
                validate_job_configuration(row)
                if active_parse_job(session, doc, exclude=row.id):
                    conflict("该文档已有活动解析任务，请先查看任务状态")
            status = "pending"
        else:
            conflict("当前任务状态不允许此操作")
        changed = session.execute(
            update(Job)
            .where(Job.id == row.id, Job.fencing_token == row.fencing_token, Job.status == row.status)
            .values(status=status, fencing_token=row.fencing_token + 1, lease_until=0, updated_at=now())
            .execution_options(synchronize_session=False)
        )
        if changed.rowcount != 1:
            conflict()
        session.refresh(row)
        if row.kind == "document.parse":
            doc = session.get(Document, row.payload["document_id"])
            if doc and doc.revision == row.payload["revision"]:
                try:
                    check_parse_baseline(doc, row)
                except DomainError:
                    pass
                else:
                    retained = retained_parse_state(doc)
                    doc.state = retained or ("cancelled" if status == "cancelled" else "pending")
                    doc.error = "解析任务已取消" if status == "cancelled" else None
                    if retained is None:
                        enqueue_content(session, doc, "document")
        elif row.kind == "source.snapshot":
            snapshot = session.get(SourceSnapshot, row.payload["snapshot_id"])
            if snapshot:
                snapshot.status, snapshot.error = (
                    ("cancelled", "采集任务已取消") if status == "cancelled" else ("pending", None)
                )
        audit(session, user, row.project_id, f"job.{operation}", row.id)
        return row_dict(row)


@router.get("/jobs/metrics")
def job_metrics(request: Request, project_id: str, user=Depends(authenticated)):
    with request.app.state.db.transaction(user.tenant_id) as session:
        ctx = content_context(session, request, user, project_id, permission="jobs:manage")
        conditions = (Job.tenant_id == user.tenant_id, Job.project_id == project_id, job_condition(ctx))
        rows = session.execute(
            select(Job.kind, Job.status, func.count()).where(*conditions).group_by(Job.kind, Job.status)
        ).all()
        oldest = session.scalar(
            select(func.min(Job.created_at)).where(*conditions, Job.status.in_(["pending", "retry_wait"]))
        )
        age = max(0, (datetime.now(UTC) - datetime.fromisoformat(oldest)).total_seconds()) if oldest else 0
    heartbeat = request.app.state.settings.data_dir / "worker.heartbeat"
    try:
        worker_age = max(0, time.time() - heartbeat.stat().st_mtime)
    except FileNotFoundError:
        worker_age = None
    return {
        "jobs": [{"kind": kind, "status": status, "count": count} for kind, status, count in rows],
        "pending": sum(count for _, status, count in rows if status in {"pending", "retry_wait"}),
        "failed": sum(count for _, status, count in rows if status == "failed"),
        "oldest_pending_seconds": round(age, 1),
        "worker_heartbeat_age_seconds": worker_age,
    }
