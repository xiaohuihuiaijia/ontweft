import time

from sqlalchemy import or_, select, update

from ontology.core.errors import DomainError, conflict
from ontology.core.models import now
from ontology.identity.models import Membership, Project, Tenant, User
from ontology.identity.security import ROLES
from ontology.jobs.models import Job


def claim(db, worker_id: str, lease_seconds=300, *, kinds=None, source_kinds=None) -> dict | None:
    with db.transaction() as session:
        tenants = list(session.scalars(select(Tenant.id)))
    for tenant_id in tenants:
        run = claim_for_tenant(
            db, tenant_id, worker_id, lease_seconds, kinds=kinds, source_kinds=source_kinds
        )
        if run:
            return run
    return None


def claim_for_tenant(db, tenant_id, worker_id, lease_seconds, *, kinds=None, source_kinds=None):
    with db.transaction(tenant_id) as session:
        current = time.time()
        eligible = (Job.tenant_id == tenant_id) & or_(
            Job.status == "pending", (Job.status == "running") & (Job.lease_until < current)
        )
        if kinds is not None:
            eligible &= Job.kind.in_(kinds)
        if source_kinds is not None:
            from ontology.sources.models import Source, SourceSnapshot

            supported = (
                select(SourceSnapshot.id)
                .join(Source, Source.id == SourceSnapshot.source_id)
                .where(Source.tenant_id == tenant_id, Source.kind.in_(source_kinds))
            )
            eligible &= or_(
                Job.kind != "source.snapshot", Job.payload["snapshot_id"].as_string().in_(supported)
            )
        job = session.scalar(
            select(Job).where(eligible).order_by(Job.created_at).with_for_update(skip_locked=True).limit(1)
        )
        if not job:
            return None
        token = job.fencing_token + 1
        changed = session.execute(
            update(Job)
            .where(Job.id == job.id, Job.fencing_token == job.fencing_token, eligible)
            .values(
                status="running",
                lease_owner=worker_id,
                lease_until=current + lease_seconds,
                fencing_token=token,
                attempts=job.attempts + 1,
                updated_at=now(),
            )
            .execution_options(synchronize_session=False)
        )
        if changed.rowcount != 1:
            return None
        return {
            "id": job.id,
            "tenant_id": job.tenant_id,
            "project_id": job.project_id,
            "kind": job.kind,
            "payload": job.payload,
            "owner_id": job.owner_id,
            "fencing_token": token,
            "lease_owner": worker_id,
        }


def check_job_authority(session, job: Job):
    session.scalar(
        select(Project)
        .where(Project.id == job.project_id, Project.tenant_id == job.tenant_id)
        .with_for_update()
    )
    if job.kind == "cdc.project":
        # CDC has both a job requester and an immutable configuration owner.
        # Lock them in the same order even across projects to avoid A->B/B->A
        # deadlocks and refresh identities loaded before a revocation commit.
        from ontology.cdc.projection_models import CdcProjectionBinding

        binding = session.get(CdcProjectionBinding, job.payload.get("binding_id"))
        users = {job.owner_id}
        if binding and binding.tenant_id == job.tenant_id and binding.project_id == job.project_id:
            users.add(binding.owner_id)
        session.scalars(
            select(User)
            .where(User.tenant_id == job.tenant_id, User.id.in_(users))
            .order_by(User.id)
            .with_for_update(key_share=True)
            .execution_options(populate_existing=True)
        ).all()
    user = session.scalar(
        select(User)
        .where(User.id == job.owner_id, User.tenant_id == job.tenant_id)
        .with_for_update(key_share=True)
        .execution_options(populate_existing=True)
    )
    member = session.scalar(
        select(Membership)
        .where(
            Membership.user_id == job.owner_id,
            Membership.project_id == job.project_id,
            Membership.tenant_id == job.tenant_id,
        )
        .with_for_update()
    )
    required = {
        "document.parse": "documents:write",
        "ai.propose": "ontology:write",
        "source.discover": "sources:write",
        "source.snapshot": "sources:write",
        "cdc.project": "sources:write",
        "model.validate": "ontology:write",
        "relation.recompute": "objects:write",
    }.get(job.kind)
    if (
        not user
        or not user.active
        or not member
        or not required
        or required not in ROLES.get(member.role, set())
    ):
        raise DomainError(403, "job_authorization_revoked", "任务执行权限已失效")
    if job.kind == "ai.propose":
        from ontology.intelligence.proposal_api import check_inputs
        from ontology.intelligence.proposal_models import AIProposal
        proposal = session.get(AIProposal, job.payload.get("proposal_id"))
        if not proposal or proposal.tenant_id != job.tenant_id or proposal.project_id != job.project_id:
            raise DomainError(403, "job_authorization_revoked", "提案范围已失效")
        return check_inputs(session, proposal, user)
    if job.kind == "document.parse":
        from ontology.content_access.service import require_job_source

        require_job_source(session, job, user, member)


def fenced_job(session, run: dict) -> Job:
    # Project -> job -> identity locks, matching API mutation ordering.
    session.scalar(
        select(Project)
        .where(Project.id == run["project_id"], Project.tenant_id == run["tenant_id"])
        .with_for_update()
    )
    job = session.scalar(
        select(Job).where(Job.id == run["id"], Job.tenant_id == run["tenant_id"]).with_for_update()
    )
    if (
        not job
        or job.status != "running"
        or job.fencing_token != run["fencing_token"]
        or job.lease_owner != run["lease_owner"]
        or job.lease_until <= time.time()
    ):
        conflict("任务租约已失效，旧执行结果不可提交")
    return job


def finish(db, run: dict, *, result=None, error=None, apply=None):
    with db.transaction(run["tenant_id"]) as session:
        job = fenced_job(session, run)
        check_job_authority(session, job)
        if apply:
            apply(session, job)
        job.status = "failed" if error else "succeeded"
        job.result, job.error = result or {}, error
        job.lease_until, job.updated_at = 0, now()
