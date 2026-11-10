"""Original, standard-library-only ontology / Function / Action example.

Run: python examples/ontology_action_demo.py
Test: python -m unittest discover -s examples -p "test_*.py" -v

SQLite demonstrates durable local transactions. Identity contexts are trusted
demo fixtures, not an authentication service. The gateway simulates tasks; it
does not call Feishu. Its lookup and idempotency guarantees are NOT Feishu APIs.

Scope: one fixed object type, no dynamic schema or relationship engine. Hashes
check consistency, not malicious database tampering. A crash after RUNNING is
committed needs operator recovery after fencing the old worker; automatic lease
recovery is not implemented. Never blindly resend a stranded RUNNING action.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import date
from typing import Callable, Protocol


ISSUE_TYPE = "work.Issue@1"
FUNCTION_ID = "work.is_overdue@1"
ACTION_ID = "work.create_followup@1"

# Definition metadata is separate from the records and Python implementations.
ONTOLOGY = {
    "namespace": "work",
    "version": 1,
    "object_types": {
        ISSUE_TYPE: {
            "properties": {"title": "string", "due_date": "date", "status": ["open", "done"]},
            "relations": {"project_id": "core.Project@1", "owner_id": "core.Person@1"},
            "functions": [FUNCTION_ID],
            "actions": [ACTION_ID],
        }
    },
}


class Forbidden(Exception):
    pass


class Conflict(Exception):
    pass


@dataclass(frozen=True)
class Issue:
    tenant_id: str
    id: str
    project_id: str
    owner_id: str
    title: str
    due_date: date
    status: str
    revision: int
    evidence: tuple[str, ...]
    type_id: str = ISSUE_TYPE


@dataclass(frozen=True)
class Principal:
    id: str
    tenant_id: str
    projects: frozenset[str]
    permissions: frozenset[str]
    active: bool = True


@dataclass(frozen=True)
class FollowupParams:
    recipient_id: str
    due_date: date


def is_overdue(issue: Issue, as_of: date) -> bool:
    """Pure Function: explicit clock, no database writes or remote calls."""
    return issue.status != "done" and issue.due_date < as_of


def build_followup(issue: Issue, params: FollowupParams) -> dict:
    """Produce the exact approved outbound payload; do not send it here."""
    return {
        "title": f"跟进：{issue.title}",
        "recipient_id": params.recipient_id,
        "due_date": params.due_date.isoformat(),
        "source_issue_id": issue.id,
    }


@dataclass(frozen=True)
class FunctionDefinition:
    target_type: str
    permission: str
    implementation: Callable


@dataclass(frozen=True)
class ActionDefinition:
    target_type: str
    request_permission: str
    approve_permission: str
    execute_permission: str
    implementation: Callable


FUNCTIONS = {FUNCTION_ID: FunctionDefinition(ISSUE_TYPE, "issue:read", is_overdue)}
ACTIONS = {ACTION_ID: ActionDefinition(
    ISSUE_TYPE, "followup:request", "followup:approve", "followup:execute", build_followup
)}


def canonical(value) -> str:
    return json.dumps(value, default=lambda x: x.isoformat(), sort_keys=True,
                      ensure_ascii=False, separators=(",", ":"))


def digest(value) -> str:
    return hashlib.sha256(canonical(value).encode("utf-8")).hexdigest()


class TaskGateway(Protocol):
    def create(self, execution_id: str, payload: dict) -> str: ...
    def lookup(self, execution_id: str) -> str | None: ...


class FakeTaskGateway:
    """Simulated service with unlimited deduplication, unlike real Feishu."""
    def __init__(self, lose_response=False):
        self.tasks: dict[str, str] = {}
        self.calls = 0
        self.lose_response = lose_response

    def create(self, execution_id: str, payload: dict) -> str:
        self.calls += 1
        self.tasks.setdefault(execution_id, f"fake-task-{len(self.tasks) + 1}")
        if self.lose_response:
            self.lose_response = False
            raise TimeoutError("remote accepted; response lost")
        return self.tasks[execution_id]

    def lookup(self, execution_id: str) -> str | None:
        return self.tasks.get(execution_id)


class Runtime:
    def __init__(self, database: str, principals: dict[str, Principal],
                 members: dict[tuple[str, str], set[str]], gateway: TaskGateway,
                 today: Callable[[], date]):
        self.db = sqlite3.connect(database, isolation_level=None, timeout=5)
        self.db.row_factory = sqlite3.Row
        self.principals, self.members, self.gateway, self.today = principals, members, gateway, today
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS issues (
                tenant_id TEXT, id TEXT, body TEXT NOT NULL,
                PRIMARY KEY (tenant_id, id)
            );
            CREATE TABLE IF NOT EXISTS runs (
                id TEXT PRIMARY KEY, tenant_id TEXT, issue_id TEXT, requester TEXT,
                action_id TEXT, idem_key TEXT, request_hash TEXT, issue_revision INTEGER,
                payload TEXT, payload_hash TEXT, state TEXT, approver TEXT,
                external_id TEXT, error TEXT,
                UNIQUE(tenant_id, action_id, idem_key)
            );
            CREATE TABLE IF NOT EXISTS audit (
                seq INTEGER PRIMARY KEY, run_id TEXT, actor TEXT, event TEXT,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            );
        """)

    @contextmanager
    def transaction(self):
        self.db.execute("BEGIN IMMEDIATE")
        try:
            yield
            self.db.commit()
        except BaseException:
            self.db.rollback()
            raise

    def seed_issue(self, issue: Issue):
        """Fixture loader only, not a public write endpoint."""
        if issue.status not in {"open", "done"} or issue.revision < 1:
            raise ValueError("invalid issue")
        self.db.execute("INSERT INTO issues VALUES (?,?,?)",
                        (issue.tenant_id, issue.id, canonical(asdict(issue))))

    def principal(self, actor: str) -> Principal:
        person = self.principals.get(actor)
        if person is None or not person.active:
            raise Forbidden("inactive or unknown identity")
        return person

    def issue(self, actor: str, issue_id: str) -> Issue:
        person = self.principal(actor)
        row = self.db.execute("SELECT body FROM issues WHERE tenant_id=? AND id=?",
                              (person.tenant_id, issue_id)).fetchone()
        if row is None:
            raise Forbidden("object unavailable")
        value = json.loads(row["body"])
        value["due_date"] = date.fromisoformat(value["due_date"])
        value["evidence"] = tuple(value["evidence"])
        issue = Issue(**value)
        self.authorize(actor, issue, "issue:read")
        return issue

    def authorize(self, actor: str, issue: Issue, permission: str):
        person = self.principal(actor)
        if (person.tenant_id != issue.tenant_id or issue.project_id not in person.projects
                or permission not in person.permissions):
            raise Forbidden("not authorized")

    def invoke_function(self, actor: str, function_id: str, issue_id: str, as_of: date):
        definition = FUNCTIONS[function_id]
        issue = self.issue(actor, issue_id)
        if issue.type_id != definition.target_type:
            raise Conflict("function target type mismatch")
        self.authorize(actor, issue, definition.permission)
        return definition.implementation(issue, as_of)

    def audit(self, run_id: str, actor: str, event: str):
        self.db.execute("INSERT INTO audit(run_id,actor,event) VALUES (?,?,?)", (run_id, actor, event))

    def get_run(self, actor: str, run_id: str) -> dict:
        row = self.db.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
        if row is None or row["tenant_id"] != self.principal(actor).tenant_id:
            raise Forbidden("run unavailable")
        self.issue(actor, row["issue_id"])
        # In this small example all project readers may view action history.
        return dict(row)

    def check_business(self, issue: Issue, payload: dict):
        if issue.type_id != ACTIONS[ACTION_ID].target_type:
            raise Conflict("action target type mismatch")
        if not is_overdue(issue, self.today()):
            raise Conflict("issue must still be open and overdue")
        if date.fromisoformat(payload["due_date"]) < self.today():
            raise Conflict("follow-up due date is in the past")
        if payload["recipient_id"] not in self.members.get((issue.tenant_id, issue.project_id), set()):
            raise Forbidden("recipient is outside the project")

    def request(self, actor: str, issue_id: str, params: FollowupParams, idem_key: str) -> str:
        if not idem_key.strip():
            raise ValueError("idempotency key required")
        definition = ACTIONS[ACTION_ID]
        fingerprint = digest({"action": ACTION_ID, "issue": issue_id, "params": asdict(params)})
        with self.transaction():
            issue = self.issue(actor, issue_id)
            self.authorize(actor, issue, definition.request_permission)
            existing = self.db.execute("SELECT * FROM runs WHERE tenant_id=? AND action_id=? AND idem_key=?",
                                       (issue.tenant_id, ACTION_ID, idem_key)).fetchone()
            if existing:
                if existing["requester"] != actor or existing["request_hash"] != fingerprint:
                    raise Conflict("idempotency key already used for a different request")
                return existing["id"]
            payload = definition.implementation(issue, params)
            self.check_business(issue, payload)
            run_id = str(uuid.uuid4())
            self.db.execute("""INSERT INTO runs
                (id,tenant_id,issue_id,requester,action_id,idem_key,request_hash,
                 issue_revision,payload,payload_hash,state)
                VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                (run_id, issue.tenant_id, issue.id, actor, ACTION_ID, idem_key,
                 fingerprint, issue.revision, canonical(payload), digest(payload), "PENDING"))
            self.audit(run_id, actor, "requested")
            return run_id

    def revalidate(self, run: dict, issue: Issue):
        if run["action_id"] != ACTION_ID or run["issue_revision"] != issue.revision:
            raise Conflict("definition or object changed; create a new reviewed request")
        payload = json.loads(run["payload"])
        if digest(payload) != run["payload_hash"]:
            raise Conflict("approved payload changed")
        self.check_business(issue, payload)
        self.authorize(run["requester"], issue, "issue:read")
        self.authorize(run["requester"], issue, ACTIONS[ACTION_ID].request_permission)

    def approve(self, actor: str, run_id: str):
        with self.transaction():
            run = self.get_run(actor, run_id)
            issue = self.issue(actor, run["issue_id"])
            self.authorize(actor, issue, ACTIONS[ACTION_ID].approve_permission)
            if actor == run["requester"]:
                raise Forbidden("self-approval is forbidden")
            if run["state"] != "PENDING":
                raise Conflict("run is not pending")
            self.revalidate(run, issue)
            self.db.execute("UPDATE runs SET state='APPROVED',approver=? WHERE id=?", (actor, run_id))
            self.audit(run_id, actor, "approved")

    def execute(self, actor: str, run_id: str) -> dict:
        with self.transaction():
            run = self.get_run(actor, run_id)
            issue = self.issue(actor, run["issue_id"])
            self.authorize(actor, issue, ACTIONS[ACTION_ID].execute_permission)
            if run["state"] == "SUCCEEDED":
                return run
            if run["state"] != "APPROVED":
                raise Conflict("run cannot be dispatched; UNKNOWN requires reconciliation")
            self.revalidate(run, issue)
            self.authorize(run["approver"], issue, "issue:read")
            self.authorize(run["approver"], issue, ACTIONS[ACTION_ID].approve_permission)
            self.db.execute("UPDATE runs SET state='RUNNING' WHERE id=?", (run_id,))
            self.audit(run_id, actor, "dispatch_started")
        # Never hold the local database transaction across an external request.
        try:
            external_id = self.gateway.create(run_id, json.loads(run["payload"]))
        except Exception as exc:
            # Once dispatch starts, an exception does not prove remote failure.
            with self.transaction():
                self.db.execute("UPDATE runs SET state='UNKNOWN',error=? WHERE id=?",
                                (type(exc).__name__, run_id))
                self.audit(run_id, actor, "outcome_unknown")
        else:
            with self.transaction():
                self.db.execute("UPDATE runs SET state='SUCCEEDED',external_id=? WHERE id=?",
                                (external_id, run_id))
                self.audit(run_id, actor, "remote_creation_confirmed")
        return self.get_run(actor, run_id)

    def reconcile(self, actor: str, run_id: str) -> dict:
        run = self.get_run(actor, run_id)
        self.authorize(actor, self.issue(actor, run["issue_id"]), "followup:execute")
        if run["state"] != "UNKNOWN":
            raise Conflict("only UNKNOWN runs can be reconciled")
        external_id = self.gateway.lookup(run_id)
        if external_id is not None:
            with self.transaction():
                self.db.execute("UPDATE runs SET state='SUCCEEDED',external_id=? WHERE id=? AND state='UNKNOWN'",
                                (external_id, run_id))
                self.audit(run_id, actor, "remote_result_reconciled")
        # No result is not proof that the remote action never happened.
        return self.get_run(actor, run_id)


def demo_runtime(database=":memory:", gateway=None) -> Runtime:
    principals = {
        "alice": Principal("alice", "enterprise-a", frozenset({"project-1"}),
                           frozenset({"issue:read", "followup:request"})),
        "bob": Principal("bob", "enterprise-a", frozenset({"project-1"}),
                         frozenset({"issue:read", "followup:approve", "followup:execute"})),
    }
    return Runtime(database, principals, {("enterprise-a", "project-1"): {"alice", "bob"}},
                   gateway if gateway is not None else FakeTaskGateway(), lambda: date(2026, 10, 28))


def sample_issue() -> Issue:
    return Issue("enterprise-a", "issue-1001", "project-1", "alice", "确认项目交付范围",
                 date(2026, 10, 25), "open", 1, ("demo://document-7/revision-2/paragraph-9",))


if __name__ == "__main__":
    runtime = demo_runtime()
    issue = sample_issue()
    runtime.seed_issue(issue)
    print("对象类型:", issue.type_id, "对象实例:", issue.id)
    print("Function 是否延期:", runtime.invoke_function("alice", FUNCTION_ID, issue.id, date(2026, 10, 28)))
    params = FollowupParams("alice", date(2026, 10, 30))
    run_id = runtime.request("alice", issue.id, params, "followup-request-001")
    print("申请状态:", runtime.get_run("alice", run_id)["state"])
    runtime.approve("bob", run_id)
    result = runtime.execute("bob", run_id)
    print("Action 结果:", result["state"], result["external_id"])
    runtime.execute("bob", run_id)
    print("重复执行后的外部调用次数:", runtime.gateway.calls)
    runtime.db.close()
