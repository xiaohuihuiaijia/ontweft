"""Bounded validation on the existing job queue, with publication delta checks."""

from sqlalchemy import delete, func, select, tuple_
from sqlalchemy.orm import aliased

from ontology.core.domain import (
    compare_and_swap,
    definition_errors,
    validate_properties,
    validate_source_properties,
)
from ontology.core.errors import DomainError, conflict
from ontology.core.models import now, uid
from ontology.identity.models import User
from ontology.identity.security import access
from ontology.jobs.models import Job
from ontology.jobs.service import check_job_authority, fenced_job
from ontology.modeling.models import ChangeSet
from ontology.objects.constraints import key_rows
from ontology.objects.models import Entity, EntityKey, Relation

BATCH = 250
SYNC_LIMIT = 1000


def entity_query(ctx):
    return select(Entity).where(Entity.tenant_id == ctx.user.tenant_id, Entity.project_id == ctx.project.id)


def add_errors(report, errors):
    report["error_count"] += len(errors)
    report["errors"] = (report["errors"] + errors)[:100]
    report["valid"] = report["error_count"] == 0


def validate_batch(session, ctx, definition, rows, report):
    types = {t["key"]: t for t in definition["types"]}
    ids = [obj.id for obj in rows]
    if not ids:
        return
    session.execute(
        delete(EntityKey).where(
            EntityKey.tenant_id == ctx.user.tenant_id,
            EntityKey.project_id == ctx.project.id,
            EntityKey.definition_id == report["generation"],
            EntityKey.entity_id.in_(ids),
        )
    )
    candidates = []
    for obj in rows:
        if obj.deleted:
            continue
        type_def = types.get(obj.type_key)
        if not type_def:
            add_errors(report, [{"object_id": obj.id, "message": "删除类型会使现有对象失去定义"}])
            continue
        try:
            (validate_properties if obj.data_status == "active" else validate_source_properties)(
                type_def, obj.properties
            )
        except DomainError:
            add_errors(report, [{"object_id": obj.id, "message": "现有对象与新定义不兼容，须先完成迁移"}])
            continue
        candidates.extend(key_rows(obj, type_def, report["generation"]))
    keys = {(row["type_key"], row["fields_hash"], row["value_hash"]) for row in candidates}
    known = {}
    # One indexed read per batch, not one read/flush per object/key.
    key_list = list(keys)
    for start in range(0, len(key_list), 250):
        existing = session.execute(
            select(
                EntityKey.type_key, EntityKey.fields_hash, EntityKey.value_hash, EntityKey.entity_id
            ).where(
                EntityKey.tenant_id == ctx.user.tenant_id,
                EntityKey.project_id == ctx.project.id,
                EntityKey.definition_id == report["generation"],
                tuple_(EntityKey.type_key, EntityKey.fields_hash, EntityKey.value_hash).in_(
                    key_list[start : start + 250]
                ),
            )
        )
        known.update({(kind, fields, value): entity for kind, fields, value, entity in existing})
    for values in candidates:
        key = values["type_key"], values["fields_hash"], values["value_hash"]
        if key in known:
            add_errors(
                report,
                [
                    {
                        "object_id": values["entity_id"],
                        "other_object_id": known[key],
                        "message": "组合唯一键重复",
                    }
                ],
            )
        else:
            known[key] = values["entity_id"]
            session.add(EntityKey(**values))
    session.flush()
    # Join endpoint types once per batch; no per-edge object lookups.
    target = aliased(Entity)
    source_types = {obj.id: obj.type_key for obj in rows}
    counts = {}
    for rel, target_type in session.execute(
        select(Relation, target.type_key)
        .outerjoin(target, target.id == Relation.target_id)
        .where(
            Relation.tenant_id == ctx.user.tenant_id,
            Relation.project_id == ctx.project.id,
            Relation.source_id.in_(ids),
        )
    ):
        expected = next(
            (
                r
                for r in types.get(source_types.get(rel.source_id), {}).get("relations", [])
                if r["key"] == rel.type_key
            ),
            None,
        )
        if not expected or expected["target_type"] != target_type:
            add_errors(report, [{"relation_id": rel.id, "message": "已有关系与新定义不兼容"}])
        elif expected.get("cardinality") == "one":
            key = rel.source_id, rel.type_key
            counts[key] = counts.get(key, 0) + 1
            if counts[key] > 1:
                add_errors(report, [{"relation_id": rel.id, "message": "现有关系不满足一个目标的基数"}])


def discard_validation(session, change):
    """Remove disposable keys and stop work tied to an obsolete draft revision."""
    old = change.validation.get("generation")
    if old:
        session.execute(
            delete(EntityKey).where(
                EntityKey.tenant_id == change.tenant_id,
                EntityKey.project_id == change.project_id,
                EntityKey.definition_id == old,
            )
        )
    job_id = change.validation.get("job_id")
    job = session.get(Job, job_id) if job_id else None
    if job and job.status in {"pending", "running"}:
        job.status, job.lease_until, job.updated_at = "cancelled", 0, now()


def start_validation(session, ctx, change):
    from ontology.intelligence.projection import document_resources_fingerprint, draft_document_resources
    from ontology.sources.ingestion import mapping_errors

    discard_validation(session, change)
    document_resources = draft_document_resources(session, change)
    errors = definition_errors(change.definition) + mapping_errors(
        session, ctx, change.definition, resource_overrides=document_resources
    )
    report = {
        "valid": not errors,
        "errors": errors[:100],
        "error_count": len(errors),
        "objects_checked": 0,
        "generation": uid(),
        "watermark": ctx.project.data_revision,
        "scope": "all_current_objects",
    }
    if document_resources:
        report["document_resources_fingerprint"] = document_resources_fingerprint(document_resources)
    total = session.scalar(
        select(func.count())
        .select_from(Entity)
        .where(Entity.tenant_id == ctx.user.tenant_id, Entity.project_id == ctx.project.id)
    )
    if errors or total <= SYNC_LIMIT:
        if not errors:
            rows = session.scalars(entity_query(ctx).order_by(Entity.id)).all()
            validate_batch(session, ctx, change.definition, rows, report)
            report["objects_checked"] = len(rows)
        return report, None
    job = Job(
        tenant_id=ctx.user.tenant_id,
        project_id=ctx.project.id,
        owner_id=ctx.user.id,
        kind="model.validate",
        payload={"change_id": change.id, "revision": change.revision + 1},
        checkpoint={"cursor": "", "phase": "scan", "report": report},
    )
    session.add(job)
    session.flush()
    return {**report, "valid": False, "job_id": job.id}, job


def recheck_delta(session, ctx, change):
    report = dict(change.validation)
    if "generation" not in report:
        conflict("请重新校验当前修订")
    if not report.get("valid"):
        if report.get("error_count"):
            raise DomainError(422, "validation_failed", "本体或现有数据不满足发布约束", report)
        conflict("请等待当前修订完成校验")
    rows = session.scalars(
        entity_query(ctx)
        .where(Entity.changed_seq > report["watermark"])
        .order_by(Entity.changed_seq, Entity.id)
        .limit(SYNC_LIMIT + 1)
    ).all()
    if len(rows) > SYNC_LIMIT:
        conflict("校验后数据变化超过同步复核预算，请重新运行后台校验")
    validate_batch(session, ctx, change.definition, rows, report)
    report["watermark"] = ctx.project.data_revision
    return report


def run_validation(db, run):
    with db.transaction(run["tenant_id"], write=True) as session:
        job = fenced_job(session, run)
        check_job_authority(session, job)
        change = session.get(ChangeSet, job.payload["change_id"])
        ctx = access(session, session.get(User, job.owner_id), job.project_id, "ontology:write")
        if (
            not change
            or change.project_id != job.project_id
            or change.revision != job.payload["revision"]
            or change.status != "validating"
        ):
            conflict("变更修订已改变，旧校验结果不会提交")
        if change.baseline_release_id != ctx.project.active_release_id:
            conflict("本体发布基线已改变，请重新创建变更集")
        checkpoint = dict(job.checkpoint)
        report = dict(checkpoint["report"])
        if checkpoint["phase"] == "scan":
            rows = session.scalars(
                entity_query(ctx).where(Entity.id > checkpoint["cursor"]).order_by(Entity.id).limit(BATCH)
            ).all()
            validate_batch(session, ctx, change.definition, rows, report)
            report["objects_checked"] += len(rows)
            if rows:
                checkpoint["cursor"] = rows[-1].id
            if len(rows) < BATCH:
                checkpoint["phase"] = "delta"
        else:
            rows = session.scalars(
                entity_query(ctx)
                .where(Entity.changed_seq > report["watermark"])
                .order_by(Entity.changed_seq, Entity.id)
                .limit(BATCH)
            ).all()
            validate_batch(session, ctx, change.definition, rows, report)
            if rows:
                report["watermark"] = rows[-1].changed_seq
            if len(rows) < BATCH:
                report["watermark"] = ctx.project.data_revision
                compare_and_swap(
                    session,
                    change,
                    change.revision,
                    {
                        "validation": report,
                        "status": "validated" if report["valid"] else "draft",
                        "approved_by": None,
                    },
                )
                job.status, job.result = "succeeded", report
        checkpoint["report"] = report
        job.checkpoint = checkpoint
        if job.status != "succeeded":
            job.status = "pending"
        job.lease_until, job.updated_at = 0, now()
