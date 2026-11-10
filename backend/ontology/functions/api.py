from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy import func, select

from ontology.core.domain import compare_and_swap, row_dict, scoped_record, wire_value
from ontology.core.errors import DomainError, conflict, forbidden, missing
from ontology.core.schemas import RevisionInput
from ontology.functions.models import FunctionRun, FunctionVersion
from ontology.functions.runtime import (
    FunctionExecution,
    FunctionInput,
    authorize_history,
    catalog_entry,
    check_current_definition,
    definition_hash,
    digest,
    inventory_gap,
    read_access,
    read_inputs,
    validate_bindings,
)
from ontology.identity.models import Membership, Project, User
from ontology.identity.security import ROLES, access, audit, authenticated
from ontology.objects.models import Entity
from ontology.objects.status import require_active_entity

router = APIRouter()


def require_active_inputs(session, ctx, object_ids, *, history=False):
    # Project read_access holds the lock also used by contribution materialization.
    # Scope the lookup before status checks; unrelated object identities remain hidden.
    for object_id in set(object_ids):
        obj = session.scalar(
            select(Entity).where(
                Entity.id == object_id,
                Entity.tenant_id == ctx.user.tenant_id,
                Entity.project_id == ctx.project.id,
                Entity.deleted.is_(False),
            )
        )
        if obj is None:
            missing()
        if history and obj.data_status != "active":
            forbidden()
        require_active_entity(obj)


def authorize_active_history(session, ctx, run):
    authorize_history(session, ctx, run)
    require_active_inputs(session, ctx, (item["object_id"] for item in run.inputs.values()), history=True)


class FunctionUpdate(FunctionInput, RevisionInput):
    pass


def manage(session, user, version_id, permission):
    row = scoped_record(session, FunctionVersion, version_id, user)
    session.scalar(
        select(Project)
        .where(Project.id == row.project_id, Project.tenant_id == user.tenant_id)
        .with_for_update()
    )
    session.refresh(row)
    participants = sorted({user.id, row.author_id, row.reviewed_by} - {None})
    list(
        session.scalars(
            select(User)
            .where(User.id.in_(participants), User.tenant_id == user.tenant_id)
            .order_by(User.id)
            .with_for_update(key_share=True)
        )
    )
    ctx = access(session, user, row.project_id, permission)
    return row, ctx


def visible_version(session, ctx, row):
    if row.status != "published" and not ({"ontology:write", "ontology:approve"} & ctx.permissions):
        forbidden()
    validate_bindings(session, ctx, row.bindings, row.unit)


def authorized_page(rows, authorize, limit, offset):
    """Paginate visible records only, retaining at most one response page."""
    items, visible_index = [], 0
    try:
        for row in rows:
            try:
                authorize(row)
            except DomainError as exc:
                if exc.status not in {403, 404, 422}:
                    raise
                continue
            if visible_index >= offset:
                if len(items) == limit:
                    return {"items": items, "next_offset": offset + limit}
                items.append(wire_value(row_dict(row)))
            visible_index += 1
        return {"items": items, "next_offset": None}
    finally:
        rows.close()


@router.get("/functions/catalog")
def catalog(request: Request, project_id: str, user=Depends(authenticated)):
    with request.app.state.db.transaction(user.tenant_id) as session:
        access(session, user, project_id)
        return {"items": [catalog_entry()], "user_code_execution": False}


@router.post("/functions", status_code=201)
def register(data: FunctionInput, request: Request, user=Depends(authenticated)):
    with request.app.state.db.transaction(user.tenant_id) as session:
        ctx = access(session, user, data.project_id, "ontology:write")
        if not ctx.project.active_release_id or data.baseline_release_id != ctx.project.active_release_id:
            conflict("登记函数需要当前已发布的本体基线")
        bindings = {key: value.model_dump() for key, value in data.bindings.items()}
        validate_bindings(session, ctx, bindings, data.unit)
        version = (
            session.scalar(
                select(func.max(FunctionVersion.version)).where(
                    FunctionVersion.tenant_id == user.tenant_id,
                    FunctionVersion.project_id == ctx.project.id,
                    FunctionVersion.key == data.key,
                )
            )
            or 0
        ) + 1
        row = FunctionVersion(
            tenant_id=user.tenant_id,
            project_id=ctx.project.id,
            key=data.key,
            label=data.label,
            version=version,
            kernel_key=data.kernel_key,
            kernel_version=data.kernel_version,
            bindings=bindings,
            unit=data.unit,
            baseline_release_id=data.baseline_release_id,
            artifact=catalog_entry(),
            author_id=user.id,
        )
        row.snapshot_hash = definition_hash(row)
        session.add(row)
        session.flush()
        audit(session, user, row.project_id, "function.registered", row.id, {"version": version})
        return row_dict(row)


@router.get("/functions")
def versions(
    request: Request,
    project_id: str,
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0, le=100000),
    user=Depends(authenticated),
):
    with request.app.state.db.transaction(user.tenant_id) as session:
        ctx = read_access(session, user, project_id)
        rows = session.scalars(
            select(FunctionVersion)
            .where(
                FunctionVersion.tenant_id == user.tenant_id,
                FunctionVersion.project_id == project_id,
            )
            .order_by(FunctionVersion.created_at, FunctionVersion.id)
            .execution_options(yield_per=50)
        )
        return authorized_page(rows, lambda row: visible_version(session, ctx, row), limit, offset)


@router.get("/functions/{version_id}")
def version_detail(version_id: str, request: Request, user=Depends(authenticated)):
    with request.app.state.db.transaction(user.tenant_id) as session:
        row = scoped_record(session, FunctionVersion, version_id, user)
        ctx = read_access(session, user, row.project_id)
        visible_version(session, ctx, row)
        return row_dict(row)


@router.patch("/functions/{version_id}")
def edit(version_id: str, data: FunctionUpdate, request: Request, user=Depends(authenticated)):
    with request.app.state.db.transaction(user.tenant_id) as session:
        row, ctx = manage(session, user, version_id, "ontology:write")
        if row.status == "published":
            conflict("已发布函数不可覆盖，请登记新版本")
        if data.project_id != row.project_id or data.key != row.key:
            raise DomainError(422, "function_identity_immutable", "不能修改函数所属项目或稳定标识")
        if data.baseline_release_id != ctx.project.active_release_id:
            conflict("本体基线已改变")
        bindings = {key: value.model_dump() for key, value in data.bindings.items()}
        validate_bindings(session, ctx, bindings, data.unit)
        compare_and_swap(
            session,
            row,
            data.expected_revision,
            {
                "label": data.label,
                "bindings": bindings,
                "unit": data.unit,
                "baseline_release_id": data.baseline_release_id,
                "artifact": catalog_entry(),
                "status": "draft",
                "author_id": user.id,
                "reviewed_by": None,
                "reviewed_hash": None,
            },
        )
        row.snapshot_hash = definition_hash(row)
        audit(session, user, row.project_id, "function.edited", row.id)
        return row_dict(row)


@router.post("/functions/{version_id}/review")
def review(version_id: str, data: RevisionInput, request: Request, user=Depends(authenticated)):
    with request.app.state.db.transaction(user.tenant_id) as session:
        row, ctx = manage(session, user, version_id, "ontology:approve")
        if row.status != "draft":
            conflict("只有草稿函数可审阅")
        if row.author_id == user.id:
            raise DomainError(403, "function_self_review", "函数作者不能审阅自己的版本")
        check_current_definition(session, ctx, row)
        compare_and_swap(
            session,
            row,
            data.expected_revision,
            {
                "status": "reviewed",
                "reviewed_by": user.id,
                "reviewed_hash": row.snapshot_hash,
            },
        )
        audit(session, user, row.project_id, "function.reviewed", row.id)
        return row_dict(row)


@router.post("/functions/{version_id}/publish")
def publish_version(version_id: str, data: RevisionInput, request: Request, user=Depends(authenticated)):
    with request.app.state.db.transaction(user.tenant_id) as session:
        row, ctx = manage(session, user, version_id, "ontology:publish")
        if row.status != "reviewed" or row.reviewed_hash != row.snapshot_hash:
            conflict("函数版本尚未通过当前配置的审阅")
        reviewer = session.get(User, row.reviewed_by)
        membership = session.scalar(
            select(Membership).where(
                Membership.tenant_id == user.tenant_id,
                Membership.project_id == row.project_id,
                Membership.user_id == row.reviewed_by,
            )
        )
        if (
            not reviewer
            or not reviewer.active
            or not membership
            or "ontology:approve" not in ROLES.get(membership.role, set())
        ):
            conflict("审阅者权限已失效，请重新审阅")
        check_current_definition(session, ctx, row)
        compare_and_swap(
            session, row, data.expected_revision, {"status": "published", "published_by": user.id}
        )
        audit(
            session, user, row.project_id, "function.published", row.id, {"snapshot_hash": row.snapshot_hash}
        )
        return row_dict(row)


@router.post("/functions/{version_id}/runs", status_code=201)
def execute(version_id: str, data: FunctionExecution, request: Request, user=Depends(authenticated)):
    with request.app.state.db.transaction(user.tenant_id) as session:
        row = scoped_record(session, FunctionVersion, version_id, user)
        ctx = read_access(session, user, row.project_id)
        session.refresh(row)
        if row.status != "published":
            raise DomainError(409, "function_not_published", "仅可执行已发布的函数版本")
        if row.revision != data.expected_function_revision or row.reviewed_hash != row.snapshot_hash:
            conflict("函数版本已改变或未保留有效审阅")
        check_current_definition(session, ctx, row)
        require_active_inputs(session, ctx, (item.object_id for item in data.objects.values()))
        inputs, parsed = read_inputs(session, ctx, row, data.objects)
        values = inventory_gap(parsed).model_dump(mode="json")
        run = FunctionRun(
            tenant_id=user.tenant_id,
            project_id=ctx.project.id,
            function_version_id=row.id,
            actor_id=user.id,
            model_release_id=ctx.project.active_release_id,
            artifact_hash=digest(row.artifact),
            inputs=inputs,
            request_hash=digest(
                {
                    "function_id": row.id,
                    "function_revision": row.revision,
                    "model_release": ctx.project.active_release_id,
                    "inputs": inputs,
                }
            ),
            result={
                "kind": "computed",
                "values": values,
                "unit": row.unit,
                "formula": row.artifact["formula"],
                "null_policy": row.artifact["null_policy"],
            },
        )
        session.add(run)
        session.flush()
        audit(session, user, row.project_id, "function.executed", run.id, {"function_version_id": row.id})
        return wire_value(row_dict(run))


@router.get("/function-runs/{run_id}")
def run_detail(run_id: str, request: Request, user=Depends(authenticated)):
    with request.app.state.db.transaction(user.tenant_id) as session:
        run = scoped_record(session, FunctionRun, run_id, user)
        ctx = read_access(session, user, run.project_id)
        authorize_active_history(session, ctx, run)
        return wire_value(row_dict(run))


@router.get("/functions/{version_id}/runs")
def run_history(
    version_id: str,
    request: Request,
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0, le=100000),
    user=Depends(authenticated),
):
    with request.app.state.db.transaction(user.tenant_id) as session:
        row = scoped_record(session, FunctionVersion, version_id, user)
        ctx = read_access(session, user, row.project_id)
        visible_version(session, ctx, row)
        runs = session.scalars(
            select(FunctionRun)
            .where(
                FunctionRun.tenant_id == user.tenant_id,
                FunctionRun.project_id == row.project_id,
                FunctionRun.function_version_id == row.id,
            )
            .order_by(FunctionRun.created_at.desc(), FunctionRun.id)
            .execution_options(yield_per=50)
        )
        return authorized_page(runs, lambda run: authorize_active_history(session, ctx, run), limit, offset)
