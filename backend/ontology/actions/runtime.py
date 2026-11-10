"""Durable, typed fixed actions. No caller-supplied URL or executable code.

An outbox is at-least-once intent storage, not proof of remote exactly-once effects.
Once sending starts, uncertain outcomes require reconciliation and are never
automatically resent. The built-in Feishu action has no live adapter by default.
"""

from __future__ import annotations

import hashlib
import ipaddress
import json
import time
from dataclasses import dataclass
from typing import Literal, Protocol
from urllib.parse import quote, urlsplit
from uuid import uuid4

import httpx
from pydantic import Field
from sqlalchemy import select, update

from ontology.actions.models import ActionAttempt, ActionOutbox, ActionSnapshot
from ontology.actions.run_models import ActionRun
from ontology.core.domain import compare_and_swap
from ontology.core.errors import DomainError, conflict, missing
from ontology.core.schemas import StrictModel
from ontology.identity.models import Membership, Project, User
from ontology.identity.security import access, audit
from ontology.objects.models import Entity
from ontology.objects.status import require_active_entity

ACTION_KEY = "feishu.create_task"
ACTION_VERSION = "1"
TOOL_VERSION = "fixed-task-contract-v1"


class TaskParameters(StrictModel):
    title: str = Field(min_length=1, max_length=200)
    body: str = Field(default="", max_length=10000)
    assignee_id: str = Field(min_length=1, max_length=36)
    shared_user_ids: list[str] = Field(default_factory=list, max_length=50)


class ActionInput(StrictModel):
    project_id: str
    action_key: Literal["feishu.create_task"] = ACTION_KEY
    target_id: str
    expected_target_revision: int = Field(ge=1)
    parameters: TaskParameters


class RequestAction(ActionInput):
    idempotency_key: str = Field(min_length=8, max_length=160, pattern=r"^[A-Za-z0-9_.:-]+$")


@dataclass(frozen=True)
class RemoteResult:
    state: Literal["remote_accepted", "completed", "unknown", "rejected"]
    receipt: str | None = None
    message: str = ""


class TaskAdapter(Protocol):
    version: str
    destination_id: str

    def send(self, frozen: dict, remote_key: str) -> RemoteResult: ...
    def reconcile(self, frozen: dict, remote_key: str) -> RemoteResult: ...


class LoopbackTaskAdapter:
    """Explicit test contract with a real local HTTP server, never a Feishu SDK.

    Only application composition/tests can construct it; API parameters cannot
    configure a destination. Do not register this test adapter in production.
    """

    version = TOOL_VERSION

    def __init__(self, base_url: str, timeout: float = 2):
        parsed = urlsplit(base_url)
        try:
            loopback = ipaddress.ip_address(parsed.hostname or "").is_loopback
        except ValueError:
            loopback = False
        if (
            not loopback
            or parsed.scheme != "http"
            or parsed.username
            or parsed.password
            or parsed.path not in {"", "/"}
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("Test adapter requires an explicit numeric loopback origin")
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.destination_id = "local-test:" + self.base_url

    def _call(self, method: str, path: str, **kwargs) -> RemoteResult:
        try:
            with (
                httpx.Client(timeout=self.timeout, follow_redirects=False, trust_env=False) as client,
                client.stream(method, self.base_url + path, **kwargs) as response,
            ):
                data = bytearray()
                for chunk in response.iter_bytes():
                    data.extend(chunk)
                    if len(data) > 65536:
                        return RemoteResult("unknown", message="response_budget_exceeded")
                if response.status_code == 404:
                    return RemoteResult("unknown", message="not_found_is_not_proof_of_no_execution")
                if not 200 <= response.status_code < 300:
                    return RemoteResult("unknown", message="remote_status_requires_reconciliation")
                body = json.loads(data)
                state, receipt = body.get("state"), body.get("receipt")
                if (
                    state not in {"remote_accepted", "completed"}
                    or not isinstance(receipt, str)
                    or not 1 <= len(receipt) <= 200
                ):
                    return RemoteResult("unknown", message="unrecognized_remote_receipt")
                return RemoteResult(state, receipt)
        except (httpx.HTTPError, ValueError, TypeError):
            return RemoteResult("unknown", message="transport_or_invalid_response")

    def send(self, frozen: dict, remote_key: str) -> RemoteResult:
        return self._call(
            "POST",
            "/tasks",
            headers={"Idempotency-Key": remote_key},
            json={"parameters": frozen["parameters"], "target_id": frozen["target_id"]},
        )

    def reconcile(self, frozen: dict, remote_key: str) -> RemoteResult:
        return self._call("GET", "/tasks/by-idempotency/" + quote(remote_key, safe=""))


def digest(value: dict) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    ).hexdigest()


def registry_for(app) -> dict[str, TaskAdapter]:
    return getattr(app.state, "action_adapters", {})


def get_adapter(registry, key) -> TaskAdapter:
    adapter = registry.get(key)
    if adapter is None:
        raise DomainError(409, "action_adapter_unconfigured", "动作执行适配器尚未配置；未向外部发送")
    return adapter


def active_user(session, user_id, tenant_id) -> User:
    user = session.scalar(
        select(User).where(User.id == user_id, User.tenant_id == tenant_id, User.active.is_(True))
    )
    if not user:
        raise DomainError(403, "action_identity_revoked", "动作参与者账号已失效")
    return user


def lock_scope(session, tenant_id, project_id, user_ids):
    """Project first, then stable identity order across different projects.

    NO KEY UPDATE coordinates disable/revision changes while allowing foreign-key
    references. Never hold these locks across a network operation.
    """
    project = session.scalar(
        select(Project).where(Project.id == project_id, Project.tenant_id == tenant_id).with_for_update()
    )
    if not project:
        missing()
    ids = sorted({value for value in user_ids if value})
    list(
        session.scalars(
            select(User)
            .where(User.tenant_id == tenant_id, User.id.in_(ids))
            .order_by(User.id)
            .with_for_update(key_share=True)
            .execution_options(populate_existing=True)
        )
    )
    list(
        session.scalars(
            select(Membership)
            .where(
                Membership.tenant_id == tenant_id,
                Membership.project_id == project_id,
                Membership.user_id.in_(ids),
            )
            .order_by(Membership.user_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    )


def run_participants(run, snapshot, actor_id=None):
    return [
        run.requester_id,
        run.approver_id,
        snapshot.executor_id,
        actor_id,
        *[item["user_id"] for item in snapshot.frozen["recipients"]],
    ]


def recipients(session, tenant_id: str, project_id: str, params: dict) -> list[dict]:
    ids = sorted({params["assignee_id"], *params["shared_user_ids"]})
    result = []
    for user_id in ids:
        user = session.scalar(
            select(User)
            .join(Membership, Membership.user_id == User.id)
            .where(
                User.id == user_id,
                User.tenant_id == tenant_id,
                User.active.is_(True),
                Membership.tenant_id == tenant_id,
                Membership.project_id == project_id,
            )
        )
        if not user:
            raise DomainError(422, "invalid_action_recipient", "接收人必须是本项目的有效成员")
        result.append({"user_id": user.id, "display_name": user.display_name})
    return result


def make_snapshot(session, user, data: ActionInput, registry) -> dict:
    lock_scope(
        session,
        user.tenant_id,
        data.project_id,
        [user.id, data.parameters.assignee_id, *data.parameters.shared_user_ids],
    )
    user = active_user(session, user.id, user.tenant_id)
    ctx = access(session, user, data.project_id, "actions:request")
    target = session.scalar(
        select(Entity).where(
            Entity.id == data.target_id,
            Entity.tenant_id == user.tenant_id,
            Entity.project_id == data.project_id,
            Entity.deleted.is_(False),
        )
    )
    if not target:
        missing()
    require_active_entity(target)
    if target.revision != data.expected_target_revision:
        conflict("目标对象版本已改变，请重新预览")
    params = data.parameters.model_dump()
    params["shared_user_ids"] = sorted(set(params["shared_user_ids"]))
    adapter = registry.get(data.action_key)
    return {
        "action_key": data.action_key,
        "action_version": ACTION_VERSION,
        "requester_id": user.id,
        "tenant_id": user.tenant_id,
        "tool_version": adapter.version if adapter else TOOL_VERSION,
        "function_versions": [],
        "destination_id": adapter.destination_id if adapter else "feishu:unconfigured",
        "project_id": ctx.project.id,
        "release_id": ctx.project.active_release_id,
        "target_id": target.id,
        "target_type": target.type_key,
        "target_revision": target.revision,
        "parameters": params,
        "recipients": recipients(session, user.tenant_id, data.project_id, params),
        "approval_rule": "single_other_project_approver",
        "effect": "external_task_creation",
        "identity_binding": "internal_user_ids; external_employee_binding_required_before_live_feishu",
    }


def snapshot_for(session, run) -> ActionSnapshot:
    row = session.scalar(
        select(ActionSnapshot).where(
            ActionSnapshot.run_id == run.id, ActionSnapshot.tenant_id == run.tenant_id
        )
    )
    if row is None:
        conflict("动作快照缺失")
    return row


def add_event(session, run, actor_id, event, details=None, fencing=0):
    session.add(
        ActionAttempt(
            tenant_id=run.tenant_id,
            project_id=run.project_id,
            run_id=run.id,
            actor_id=actor_id,
            event=event,
            fencing_token=fencing,
            details=details or {},
        )
    )


def check_execution(session, run, snapshot, registry):
    lock_scope(session, run.tenant_id, run.project_id, run_participants(run, snapshot))
    frozen = snapshot.frozen
    requester = active_user(session, run.requester_id, run.tenant_id)
    ctx = access(session, requester, run.project_id, "actions:request")
    if not run.approver_id or run.approver_id == run.requester_id:
        raise DomainError(403, "self_approval_forbidden", "外部动作不能由申请人自审")
    approver = active_user(session, run.approver_id, run.tenant_id)
    access(session, approver, run.project_id, "actions:approve")
    executor = active_user(session, snapshot.executor_id, run.tenant_id)
    access(session, executor, run.project_id, "actions:execute")
    target = session.scalar(
        select(Entity).where(
            Entity.id == run.target_id,
            Entity.tenant_id == run.tenant_id,
            Entity.project_id == run.project_id,
            Entity.deleted.is_(False),
        )
    )
    if (
        not target
        or target.revision != run.target_revision
        or ctx.project.active_release_id != frozen["release_id"]
    ):
        raise DomainError(409, "action_requires_reapproval", "目标或模型版本已改变，需要重新申请审批")
    require_active_entity(target)
    adapter = get_adapter(registry, run.action_key)
    if (
        frozen["action_version"] != ACTION_VERSION
        or frozen["tool_version"] != adapter.version
        or frozen["destination_id"] != adapter.destination_id
    ):
        raise DomainError(409, "action_requires_reapproval", "动作版本或目的地已改变，需要重新审批")
    if (
        digest(frozen) != snapshot.approval_hash
        or digest(frozen) != run.request_hash
        or frozen["parameters"] != run.parameters
        or frozen["target_revision"] != run.target_revision
        or frozen["target_id"] != run.target_id
        or frozen["action_key"] != run.action_key
        or frozen["project_id"] != run.project_id
        or frozen["tenant_id"] != run.tenant_id
        or frozen["requester_id"] != run.requester_id
        or frozen["remote_idempotency_key"] != run.id
        or frozen["client_idempotency_key"] != run.idempotency_key
    ):
        raise DomainError(409, "action_requires_reapproval", "审批快照已改变")
    # Names are presentation metadata; effective membership and identity must remain valid.
    recipients(session, run.tenant_id, run.project_id, run.parameters)
    return adapter


def claim_action(db, tenant_id, worker_id, lease_seconds=30):
    """Claim an unsent intent; expired sending is uncertain, never resendable."""
    with db.transaction(tenant_id) as session:
        projects = list(
            session.scalars(
                select(ActionOutbox.project_id)
                .where(
                    ActionOutbox.tenant_id == tenant_id,
                    ActionOutbox.state.in_(["pending", "leased", "sending"]),
                )
                .distinct()
                .limit(100)
            )
        )
    for project_id in projects:
        claimed = _claim_project(db, tenant_id, project_id, worker_id, lease_seconds)
        if claimed:
            return claimed
    return None


def _claim_project(db, tenant_id, project_id, worker_id, lease_seconds):
    with db.transaction(tenant_id) as session:
        project = session.scalar(
            select(Project)
            .where(Project.id == project_id, Project.tenant_id == tenant_id)
            .with_for_update(skip_locked=True)
        )
        if project is None:
            return None
        current = time.time()
        expired = session.scalars(
            select(ActionOutbox)
            .where(
                ActionOutbox.tenant_id == tenant_id,
                ActionOutbox.project_id == project_id,
                ActionOutbox.state == "sending",
                ActionOutbox.lease_until < current,
            )
            .with_for_update(skip_locked=True)
        ).all()
        for box in expired:
            box.state, box.fencing_token, box.updated_at = "unknown", box.fencing_token + 1, current
            run = session.get(ActionRun, box.run_id)
            run.status, run.error, run.revision = (
                "unknown",
                "发送进程失联，需要核对远端结果",
                run.revision + 1,
            )
            add_event(session, run, run.requester_id, "sending_lease_expired", fencing=box.fencing_token)
        eligible = (ActionOutbox.state == "pending") | (
            (ActionOutbox.state == "leased") & (ActionOutbox.lease_until < current)
        )
        box = session.scalar(
            select(ActionOutbox)
            .where(ActionOutbox.tenant_id == tenant_id, ActionOutbox.project_id == project_id, eligible)
            .order_by(ActionOutbox.created_at)
            .with_for_update(skip_locked=True)
            .limit(1)
        )
        if box is None:
            return None
        token = box.fencing_token + 1
        changed = session.execute(
            update(ActionOutbox)
            .where(ActionOutbox.id == box.id, ActionOutbox.fencing_token == box.fencing_token, eligible)
            .values(
                state="leased",
                fencing_token=token,
                lease_owner=worker_id,
                lease_until=current + lease_seconds,
                updated_at=current,
            )
            .execution_options(synchronize_session=False)
        )
        if changed.rowcount != 1:
            return None
        return {
            "outbox_id": box.id,
            "tenant_id": tenant_id,
            "project_id": project_id,
            "run_id": box.run_id,
            "fencing_token": token,
            "worker_id": worker_id,
        }


def fenced_box(session, claim, expected_state):
    session.scalar(
        select(Project)
        .where(Project.id == claim["project_id"], Project.tenant_id == claim["tenant_id"])
        .with_for_update()
    )
    box = session.scalar(
        select(ActionOutbox)
        .where(ActionOutbox.id == claim["outbox_id"], ActionOutbox.tenant_id == claim["tenant_id"])
        .with_for_update()
    )
    if (
        not box
        or box.fencing_token != claim["fencing_token"]
        or box.lease_owner != claim["worker_id"]
        or box.lease_until <= time.time()
        or box.state != expected_state
    ):
        conflict("动作执行租约失效；不能提交旧结果")
    return box


def execute_claim(db, claim, registry):
    with db.transaction(claim["tenant_id"]) as session:
        box = fenced_box(session, claim, "leased")
        run = session.get(ActionRun, box.run_id)
        snapshot = snapshot_for(session, run)
        try:
            adapter = check_execution(session, run, snapshot, registry)
        except DomainError as exc:
            box.state, box.lease_until = "blocked", 0
            run.status, run.error, run.revision = "blocked", exc.message, run.revision + 1
            add_event(
                session, run, run.requester_id, "execution_blocked", {"code": exc.code}, box.fencing_token
            )
            return "blocked"
        box.state, box.attempts, box.updated_at = "sending", box.attempts + 1, time.time()
        run.status, run.revision = "sending", run.revision + 1
        frozen, remote_key = dict(snapshot.frozen), run.id
        add_event(
            session,
            run,
            snapshot.executor_id,
            "send_started",
            {"destination_id": adapter.destination_id},
            box.fencing_token,
        )
    # No database transaction is held across the external operation.
    try:
        result = adapter.send(frozen, remote_key)
    except Exception:  # noqa: BLE001 - after durable send-start any unexpected adapter failure is uncertain
        result = RemoteResult("unknown", message="adapter_failed_after_send_started")
    with db.transaction(claim["tenant_id"]) as session:
        box = fenced_box(session, claim, "sending")
        run = session.get(ActionRun, box.run_id)
        box.state, box.lease_until, box.updated_at = result.state, 0, time.time()
        run.status, run.revision = result.state, run.revision + 1
        run.result = {
            "remote_idempotency_key": run.id,
            "receipt": result.receipt,
            "business_completed": result.state == "completed",
        }
        run.error = result.message or None
        add_event(
            session,
            run,
            run.requester_id,
            "send_result",
            {"state": result.state, "receipt": result.receipt},
            box.fencing_token,
        )
    return result.state


def process_one_action(db, tenant_id, registry, worker_id=None):
    claim = claim_action(db, tenant_id, worker_id or str(uuid4()))
    if not claim:
        return False
    try:
        execute_claim(db, claim, registry)
    except DomainError as exc:
        if exc.status != 409:
            raise
        # Cancellation or replacement fenced this worker. Do not resend or
        # overwrite the newer state. Expired sending is reconciled next tick.
    return True


def reconcile_action(db, tenant_id, run_id, user, expected_revision, registry):
    # Reconciliation is read-only externally; retain current caller/project authority.
    with db.transaction(tenant_id) as session:
        run = session.scalar(
            select(ActionRun).where(ActionRun.id == run_id, ActionRun.tenant_id == tenant_id)
        )
        if not run:
            missing()
        snapshot = snapshot_for(session, run)
        lock_scope(session, tenant_id, run.project_id, run_participants(run, snapshot, user.id))
        current_user = active_user(session, user.id, tenant_id)
        access(session, current_user, run.project_id, "actions:execute")
        session.refresh(run)
        if run.revision != expected_revision or run.status not in {"unknown", "remote_accepted"}:
            conflict("仅可核对当前版本的未知或远端已接受动作")
        adapter = get_adapter(registry, run.action_key)
        if (
            adapter.destination_id != snapshot.frozen["destination_id"]
            or adapter.version != snapshot.frozen["tool_version"]
        ):
            conflict("原目的地适配器版本不可用，需人工核对")
        frozen = dict(snapshot.frozen)
    try:
        result = adapter.reconcile(frozen, run_id)
    except Exception:  # noqa: BLE001 - a failed read must never authorize a second external write
        result = RemoteResult("unknown", message="reconciliation_unavailable")
    with db.transaction(tenant_id) as session:
        run = session.scalar(
            select(ActionRun).where(ActionRun.id == run_id, ActionRun.tenant_id == tenant_id)
        )
        snapshot = snapshot_for(session, run)
        lock_scope(session, tenant_id, run.project_id, run_participants(run, snapshot, user.id))
        current_user = active_user(session, user.id, tenant_id)
        access(session, current_user, run.project_id, "actions:execute")
        compare_and_swap(
            session,
            run,
            expected_revision,
            {
                "status": result.state,
                "result": {
                    "remote_idempotency_key": run.id,
                    "receipt": result.receipt,
                    "business_completed": result.state == "completed",
                },
                "error": result.message or None,
            },
        )
        box = session.scalar(select(ActionOutbox).where(ActionOutbox.run_id == run.id))
        box.state, box.updated_at = result.state, time.time()
        add_event(session, run, user.id, "reconciled", {"state": result.state, "receipt": result.receipt})
        audit(session, user, run.project_id, "action.reconciled", run.id, {"state": result.state})
        return {"id": run.id, "status": run.status, "revision": run.revision, "result": run.result}
