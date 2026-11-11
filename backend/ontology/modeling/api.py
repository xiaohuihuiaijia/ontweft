from fastapi import APIRouter, Depends, Request
from sqlalchemy import delete, select, update

from ontology.core.domain import (
    active_definition,
    compare_and_swap,
    row_dict,
    scoped_record,
)
from ontology.core.errors import DomainError, conflict
from ontology.core.schemas import RevisionInput
from ontology.identity.models import Membership, Project, User
from ontology.identity.security import ROLES, access, allowed_field, audit, authenticated
from ontology.modeling.models import ChangeSet, Release
from ontology.modeling.schemas import ChangeSetInput, ChangeSetUpdate
from ontology.modeling.templates import starter_templates
from ontology.modeling.validation import discard_validation, recheck_delta, start_validation
from ontology.objects.models import EntityKey

router = APIRouter()


@router.get("/ontology/templates")
def templates(request: Request, project_id: str, user=Depends(authenticated)):
    with request.app.state.db.transaction(user.tenant_id) as session:
        access(session, user, project_id, "ontology:write")
        items = starter_templates()
        return {"items": items, "total": len(items)}


@router.get("/ontology/definition")
def definition(request: Request, project_id: str, user=Depends(authenticated)):
    with request.app.state.db.transaction(user.tenant_id) as session:
        ctx = access(session, user, project_id)
        if not ({"ontology:write", "ontology:approve", "ontology:publish"} & ctx.permissions):
            raise DomainError(403, "forbidden", "无权查看完整管理定义")
        return {
            "definition": active_definition(session, ctx.project),
            "release_id": ctx.project.active_release_id,
            "version": ctx.project.version,
        }


@router.get("/ontology/types")
def types(request: Request, project_id: str, user=Depends(authenticated)):
    with request.app.state.db.transaction(user.tenant_id) as session:
        ctx = access(session, user, project_id)
        items = active_definition(session, ctx.project)["types"]
        visible = [
            {
                **t,
                "properties": [p for p in t.get("properties", []) if allowed_field(p, ctx.role)],
                "unique_keys": [
                    fields
                    for fields in t.get("unique_keys", [])
                    if all(
                        any(p["key"] == field and allowed_field(p, ctx.role) for p in t["properties"])
                        for field in fields
                    )
                ],
            }
            for t in items
        ]
        return {
            "items": visible,
            "total": len(visible),
            "release_id": ctx.project.active_release_id,
            "version": ctx.project.version,
        }


@router.get("/changesets")
def changesets(request: Request, project_id: str, user=Depends(authenticated)):
    with request.app.state.db.transaction(user.tenant_id) as session:
        ctx = access(session, user, project_id)
        if not ({"ontology:write", "ontology:approve", "ontology:publish"} & ctx.permissions):
            raise DomainError(403, "forbidden", "无权查看本体变更")
        items = session.scalars(
            select(ChangeSet)
            .where(ChangeSet.tenant_id == user.tenant_id, ChangeSet.project_id == project_id)
            .order_by(ChangeSet.created_at.desc())
        ).all()
        return {"items": [row_dict(x) for x in items], "total": len(items)}


def definition_input(value, previous):
    result = value.model_dump()
    known = {item["key"]: item for item in previous.get("types", [])}
    for submitted, item in zip(value.types, result["types"], strict=True):
        if "key_semantics" not in submitted.model_fields_set and item["key"] in known:
            item["key_semantics"] = known[item["key"]].get("key_semantics", "legacy")
    return result


@router.post("/changesets", status_code=201)
def create_changeset(data: ChangeSetInput, request: Request, user=Depends(authenticated)):
    with request.app.state.db.transaction(user.tenant_id) as session:
        ctx = access(session, user, data.project_id, "ontology:write")
        if ctx.project.active_release_id != data.expected_baseline_release_id:
            conflict("草稿所依据的发布版已改变，请刷新完整定义再创建变更集")
        change = ChangeSet(
            tenant_id=user.tenant_id,
            project_id=data.project_id,
            title=data.title,
            definition=definition_input(data.definition, active_definition(session, ctx.project)),
            author_id=user.id,
            baseline_release_id=ctx.project.active_release_id,
        )
        session.add(change)
        session.flush()
        audit(session, user, data.project_id, "ontology.drafted", change.id)
        return row_dict(change)


@router.get("/changesets/{change_id}")
def get_changeset(change_id: str, request: Request, user=Depends(authenticated)):
    with request.app.state.db.transaction(user.tenant_id) as session:
        change = scoped_record(session, ChangeSet, change_id, user)
        ctx = access(session, user, change.project_id)
        if not ({"ontology:write", "ontology:approve", "ontology:publish"} & ctx.permissions):
            raise DomainError(403, "forbidden", "无权查看本体变更")
        result = row_dict(change)
        if change.validation.get("job_id"):
            from ontology.jobs.models import Job

            job = session.get(Job, change.validation["job_id"])
            if job and job.tenant_id == user.tenant_id and job.project_id == change.project_id:
                result["validation_job"] = {
                    "id": job.id,
                    "status": job.status,
                    "error": job.error,
                    "objects_checked": job.checkpoint.get("report", {}).get("objects_checked", 0),
                }
        return result


@router.patch("/changesets/{change_id}")
def edit_changeset(change_id: str, data: ChangeSetUpdate, request: Request, user=Depends(authenticated)):
    with request.app.state.db.transaction(user.tenant_id, write=True) as session:
        change = scoped_record(session, ChangeSet, change_id, user)
        access(session, user, change.project_id, "ontology:write")
        session.refresh(change)
        if change.status == "published":
            conflict("已发布版本不可修改，请创建新的变更集")
        discard_validation(session, change)
        values = {"status": "draft", "validation": {}, "approved_by": None, "author_id": user.id}
        if data.definition is not None:
            values["definition"] = definition_input(data.definition, change.definition)
        if data.title is not None:
            values["title"] = data.title
        compare_and_swap(session, change, data.expected_revision, values)
        audit(session, user, change.project_id, "ontology.edited", change.id, {"revision": change.revision})
        return row_dict(change)


@router.post("/changesets/{change_id}/{operation}")
def transition(
    change_id: str, operation: str, data: RevisionInput, request: Request, user=Depends(authenticated)
):
    permission = {
        "validate": "ontology:write",
        "approve": "ontology:approve",
        "publish": "ontology:publish",
    }.get(operation)
    if permission is None:
        raise DomainError(404, "not_found", "操作不存在")
    with request.app.state.db.transaction(user.tenant_id) as session:
        change = scoped_record(session, ChangeSet, change_id, user)
        # Membership changes use the same project lock. Acquire participating
        # user locks in one order before access() so disabling an approver cannot
        # race the final release commit, including across different projects.
        session.scalar(
            select(Project)
            .where(Project.id == change.project_id, Project.tenant_id == user.tenant_id)
            .with_for_update()
        )
        session.refresh(change)
        participants = sorted({user.id, change.approved_by} - {None})
        list(
            session.scalars(
                select(User)
                .where(User.id.in_(participants), User.tenant_id == user.tenant_id)
                .order_by(User.id)
                .with_for_update(key_share=True)
            )
        )
        ctx = access(session, user, change.project_id, permission)
        if change.revision != data.expected_revision or change.status == "published":
            conflict()
        if change.baseline_release_id != ctx.project.active_release_id:
            conflict("本体基线已改变，请基于最新发布版重新创建变更集")
        if operation == "validate":
            report, job = start_validation(session, ctx, change)
            values = {
                "validation": report,
                "status": "validating" if job else "validated" if report["valid"] else "draft",
                "approved_by": None,
            }
        else:
            report = recheck_delta(session, ctx, change)
            values = {"validation": report}
            if not report["valid"]:
                raise DomainError(422, "validation_failed", "本体或现有数据不满足发布约束", report)
            if operation == "approve":
                if request.app.state.settings.separate_model_approval and change.author_id == user.id:
                    raise DomainError(
                        403, "separate_reviewer_required", "当前修订须由另一位有审批权限的成员审核"
                    )
                if change.status != "validated":
                    conflict("请先完成当前修订的校验")
                values.update(status="approved", approved_by=user.id)
            else:
                if change.status != "approved" or not change.approved_by:
                    conflict("请先审批当前修订")
                if (
                    request.app.state.settings.separate_model_approval
                    and change.approved_by == change.author_id
                ):
                    conflict("企业模式要求另一位成员重新审批当前修订")
                reviewer = session.get(User, change.approved_by)
                membership = session.scalar(
                    select(Membership).where(
                        Membership.tenant_id == user.tenant_id,
                        Membership.project_id == change.project_id,
                        Membership.user_id == change.approved_by,
                    )
                )
                if (
                    not reviewer
                    or not reviewer.active
                    or not membership
                    or "ontology:approve" not in ROLES.get(membership.role, set())
                ):
                    conflict("审批者的当前权限已失效，请重新校验并审批该变更集")
                previous_model = active_definition(session, ctx.project)
                release = Release(
                    id=report["generation"],
                    tenant_id=user.tenant_id,
                    project_id=change.project_id,
                    version=ctx.project.version + 1,
                    definition=change.definition,
                    changeset_id=change.id,
                    published_by=user.id,
                )
                session.add(release)
                session.flush()
                query = (
                    update(Project)
                    .where(
                        Project.id == ctx.project.id,
                        Project.tenant_id == user.tenant_id,
                        Project.version == ctx.project.version,
                    )
                    .values(active_release_id=release.id, version=release.version)
                )
                if session.execute(query).rowcount != 1:
                    conflict("发布基线已变化")
                from ontology.objects.contributions.decisions import recheck_decisions_after_publish
                from ontology.objects.relation_rules import recheck_rules_after_publish

                session.refresh(ctx.project)
                recheck_decisions_after_publish(session, ctx)
                recheck_rules_after_publish(session, ctx, previous_model)
                session.execute(
                    delete(EntityKey).where(
                        EntityKey.tenant_id == user.tenant_id,
                        EntityKey.project_id == change.project_id,
                        EntityKey.definition_id != release.id,
                    )
                )
                values.update(status="published", release_id=release.id)
        compare_and_swap(session, change, data.expected_revision, values)
        audit(
            session,
            user,
            change.project_id,
            f"ontology.{operation}",
            change.id,
            {"revision": change.revision},
        )
        return row_dict(change)


@router.get("/releases")
def releases(request: Request, project_id: str, user=Depends(authenticated)):
    with request.app.state.db.transaction(user.tenant_id) as session:
        access(session, user, project_id)
        rows = session.scalars(
            select(Release)
            .where(Release.tenant_id == user.tenant_id, Release.project_id == project_id)
            .order_by(Release.version.desc())
        ).all()
        return {"items": [row_dict(row, exclude=("definition",)) for row in rows], "total": len(rows)}
