import time

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from ontology.actions.models import ActionAttempt, ActionOutbox, ActionSnapshot
from ontology.actions.run_models import ActionRun
from ontology.actions.runtime import (
    ACTION_KEY,
    ACTION_VERSION,
    ActionInput,
    RequestAction,
    active_user,
    add_event,
    check_execution,
    digest,
    lock_scope,
    make_snapshot,
    reconcile_action,
    registry_for,
    run_participants,
    snapshot_for,
)
from ontology.core.domain import compare_and_swap, row_dict
from ontology.core.errors import DomainError, conflict, missing
from ontology.core.schemas import RevisionInput
from ontology.identity.security import access, audit, authenticated

router = APIRouter()


def load_run(session, run_id, user):
    run = session.scalar(
        select(ActionRun).where(ActionRun.id == run_id, ActionRun.tenant_id == user.tenant_id)
    )
    if not run:
        missing()
    snapshot = snapshot_for(session, run)
    lock_scope(session, user.tenant_id, run.project_id, run_participants(run, snapshot, user.id))
    user = active_user(session, user.id, user.tenant_id)
    session.refresh(run)
    ctx = access(session, user, run.project_id)
    if run.requester_id != user.id and not (
        {"actions:approve", "actions:execute", "audit:read"} & ctx.permissions
    ):
        missing()
    return run, ctx


def run_view(session, run):
    result = row_dict(run)
    snapshot = snapshot_for(session, run)
    result["snapshot"] = snapshot.frozen
    result["approval_hash"] = snapshot.approval_hash
    result["approved_at"] = snapshot.approved_at
    box = session.scalar(
        select(ActionOutbox).where(ActionOutbox.run_id == run.id, ActionOutbox.tenant_id == run.tenant_id)
    )
    result["outbox"] = None if box is None else {"state": box.state, "attempts": box.attempts}
    return result


def same_request(run, snapshot, data, user):
    params = data.parameters.model_dump()
    params["shared_user_ids"] = sorted(set(params["shared_user_ids"]))
    if (
        run.requester_id != user.id
        or run.action_key != data.action_key
        or run.target_id != data.target_id
        or run.target_revision != data.expected_target_revision
        or snapshot.frozen["parameters"] != params
    ):
        raise DomainError(409, "idempotency_conflict", "幂等键已绑定其他申请或参数；不能覆盖或重新发送")


@router.get("/actions/definitions")
def definitions(request: Request, project_id: str, user=Depends(authenticated)):
    with request.app.state.db.transaction(user.tenant_id) as session:
        access(session, user, project_id, "actions:request")
        adapter = registry_for(request.app).get(ACTION_KEY)
        return {
            "items": [
                {
                    "key": ACTION_KEY,
                    "version": ACTION_VERSION,
                    "label": "创建飞书任务",
                    "input_schema": ActionInput.model_json_schema(),
                    "approval": "single_other_project_approver",
                    "effect": "external_write",
                    "configured": adapter is not None,
                    "destination_id": adapter.destination_id if adapter else "feishu:unconfigured",
                    "live_feishu_verified": False,
                }
            ],
            "total": 1,
        }


@router.post("/actions/preview")
def preview(data: ActionInput, request: Request, user=Depends(authenticated)):
    with request.app.state.db.transaction(user.tenant_id) as session:
        snapshot = make_snapshot(session, user, data, registry_for(request.app))
        return {
            "snapshot": snapshot,
            "snapshot_hash": digest(snapshot),
            "remote_send_count": 0,
            "execution_configured": data.action_key in registry_for(request.app),
        }


@router.post("/actions", status_code=201)
def request_action(data: RequestAction, request: Request, user=Depends(authenticated)):
    db = request.app.state.db
    try:
        with db.transaction(user.tenant_id) as session:
            access(session, user, data.project_id, "actions:request")
            existing = session.scalar(
                select(ActionRun).where(
                    ActionRun.tenant_id == user.tenant_id,
                    ActionRun.project_id == data.project_id,
                    ActionRun.idempotency_key == data.idempotency_key,
                )
            )
            if existing:
                snapshot = snapshot_for(session, existing)
                lock_scope(
                    session, user.tenant_id, data.project_id, run_participants(existing, snapshot, user.id)
                )
                active_user(session, user.id, user.tenant_id)
                same_request(existing, snapshot_for(session, existing), data, user)
                return run_view(session, existing)
            frozen = make_snapshot(session, user, data, registry_for(request.app))
            run = ActionRun(
                tenant_id=user.tenant_id,
                project_id=data.project_id,
                action_key=data.action_key,
                target_id=data.target_id,
                target_revision=data.expected_target_revision,
                parameters=frozen["parameters"],
                request_hash=digest(frozen),
                idempotency_key=data.idempotency_key,
                requester_id=user.id,
                status="requested",
            )
            session.add(run)
            session.flush()
            frozen = {
                **frozen,
                "remote_idempotency_key": run.id,
                "client_idempotency_key": run.idempotency_key,
            }
            run.request_hash = digest(frozen)
            session.add(
                ActionSnapshot(
                    tenant_id=user.tenant_id, project_id=data.project_id, run_id=run.id, frozen=frozen
                )
            )
            add_event(session, run, user.id, "requested", {"snapshot_hash": run.request_hash})
            audit(
                session,
                user,
                data.project_id,
                "action.requested",
                run.id,
                {"snapshot_hash": run.request_hash},
            )
            session.flush()
            return run_view(session, run)
    except IntegrityError:
        # Concurrent identical requests converge through the persisted unique key.
        with db.transaction(user.tenant_id) as session:
            access(session, user, data.project_id, "actions:request")
            existing = session.scalar(
                select(ActionRun).where(
                    ActionRun.tenant_id == user.tenant_id,
                    ActionRun.project_id == data.project_id,
                    ActionRun.idempotency_key == data.idempotency_key,
                )
            )
            if not existing:
                raise
            snapshot = snapshot_for(session, existing)
            lock_scope(
                session, user.tenant_id, data.project_id, run_participants(existing, snapshot, user.id)
            )
            active_user(session, user.id, user.tenant_id)
            same_request(existing, snapshot_for(session, existing), data, user)
            return run_view(session, existing)


@router.get("/actions")
def actions(
    request: Request,
    project_id: str,
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    user=Depends(authenticated),
):
    with request.app.state.db.transaction(user.tenant_id) as session:
        ctx = access(session, user, project_id)
        conditions = [ActionRun.tenant_id == user.tenant_id, ActionRun.project_id == project_id]
        if not ({"actions:approve", "actions:execute", "audit:read"} & ctx.permissions):
            conditions.append(ActionRun.requester_id == user.id)
        rows = session.scalars(
            select(ActionRun)
            .where(*conditions)
            .order_by(ActionRun.created_at.desc())
            .offset(offset)
            .limit(limit)
        ).all()
        return {
            "items": [row_dict(r) for r in rows],
            "total": session.scalar(select(func.count()).select_from(ActionRun).where(*conditions)),
        }


@router.get("/actions/{run_id}")
def action_detail(run_id: str, request: Request, user=Depends(authenticated)):
    with request.app.state.db.transaction(user.tenant_id) as session:
        run, _ = load_run(session, run_id, user)
        result = run_view(session, run)
        result["events"] = [
            row_dict(e)
            for e in session.scalars(
                select(ActionAttempt)
                .where(ActionAttempt.run_id == run.id, ActionAttempt.tenant_id == user.tenant_id)
                .order_by(ActionAttempt.created_at, ActionAttempt.id)
            )
        ]
        return result


@router.post("/actions/{run_id}/approve")
def approve(run_id: str, data: RevisionInput, request: Request, user=Depends(authenticated)):
    with request.app.state.db.transaction(user.tenant_id) as session:
        run, _ = load_run(session, run_id, user)
        access(session, user, run.project_id, "actions:approve")
        if run.requester_id == user.id:
            raise DomainError(403, "self_approval_forbidden", "外部动作申请人不能审批自己的申请")
        if run.status != "requested":
            conflict("当前动作状态不可审批")
        snapshot = snapshot_for(session, run)
        requester = active_user(session, run.requester_id, run.tenant_id)
        current = make_snapshot(
            session,
            requester,
            ActionInput(
                project_id=run.project_id,
                action_key=run.action_key,
                target_id=run.target_id,
                expected_target_revision=run.target_revision,
                parameters=run.parameters,
            ),
            registry_for(request.app),
        )
        current = {**current, "remote_idempotency_key": run.id, "client_idempotency_key": run.idempotency_key}
        if digest(current) != run.request_hash or digest(snapshot.frozen) != run.request_hash:
            conflict("预览内容、目标或适配器已改变，需重新申请")
        compare_and_swap(session, run, data.expected_revision, {"status": "approved", "approver_id": user.id})
        snapshot.approval_hash, snapshot.approved_at = run.request_hash, time.time()
        add_event(session, run, user.id, "approved", {"snapshot_hash": run.request_hash})
        audit(session, user, run.project_id, "action.approved", run.id, {"snapshot_hash": run.request_hash})
        return run_view(session, run)


@router.post("/actions/{run_id}/execute")
def enqueue(run_id: str, data: RevisionInput, request: Request, user=Depends(authenticated)):
    with request.app.state.db.transaction(user.tenant_id) as session:
        run, _ = load_run(session, run_id, user)
        access(session, user, run.project_id, "actions:execute")
        if run.status != "approved":
            conflict("只有当前已批准的动作可以排队；未知结果不得重发")
        snapshot = snapshot_for(session, run)
        snapshot.executor_id = user.id
        check_execution(session, run, snapshot, registry_for(request.app))
        compare_and_swap(session, run, data.expected_revision, {"status": "queued"})
        session.add(
            ActionOutbox(
                tenant_id=run.tenant_id,
                project_id=run.project_id,
                run_id=run.id,
                state="pending",
                updated_at=time.time(),
            )
        )
        add_event(session, run, user.id, "queued")
        audit(session, user, run.project_id, "action.queued", run.id)
        session.flush()
        return run_view(session, run)


@router.post("/actions/{run_id}/reject")
def reject(run_id: str, data: RevisionInput, request: Request, user=Depends(authenticated)):
    with request.app.state.db.transaction(user.tenant_id) as session:
        run, _ = load_run(session, run_id, user)
        access(session, user, run.project_id, "actions:approve")
        if run.status not in {"requested", "approved"}:
            conflict("当前状态不能拒绝；已发送动作需独立处理")
        compare_and_swap(session, run, data.expected_revision, {"status": "rejected"})
        add_event(session, run, user.id, "rejected")
        audit(session, user, run.project_id, "action.rejected", run.id)
        return run_view(session, run)


@router.post("/actions/{run_id}/withdraw")
def withdraw(run_id: str, data: RevisionInput, request: Request, user=Depends(authenticated)):
    with request.app.state.db.transaction(user.tenant_id) as session:
        run, _ = load_run(session, run_id, user)
        if run.requester_id != user.id:
            raise DomainError(403, "forbidden", "只有申请人可以撤回")
        if run.status not in {"requested", "approved", "queued"}:
            conflict("已开始发送或结果未知的动作不能通过撤回假装撤销远端效果")
        box = session.scalar(select(ActionOutbox).where(ActionOutbox.run_id == run.id).with_for_update())
        if box:
            if box.state not in {"pending", "leased"}:
                conflict("发送已开始，不能撤回")
            box.state, box.fencing_token, box.lease_until = "cancelled", box.fencing_token + 1, 0
        compare_and_swap(session, run, data.expected_revision, {"status": "cancelled"})
        add_event(session, run, user.id, "withdrawn")
        audit(session, user, run.project_id, "action.withdrawn", run.id)
        return run_view(session, run)


@router.post("/actions/{run_id}/reconcile")
def reconcile(run_id: str, data: RevisionInput, request: Request, user=Depends(authenticated)):
    return reconcile_action(
        request.app.state.db, user.tenant_id, run_id, user, data.expected_revision, registry_for(request.app)
    )
