"""Action tests use a real loopback HTTP server, never a live Feishu account."""

import time
from concurrent.futures import ThreadPoolExecutor

import pytest
from ontology.actions.models import ActionOutbox
from ontology.actions.run_models import ActionRun
from ontology.actions.runtime import (
    claim_action,
    execute_claim,
    process_one_action,
)
from ontology.core.errors import DomainError
from ontology.identity.models import Membership, User
from ontology.worker import run_worker
from sqlalchemy import select

from tests.support.actions import action_platform as action_platform  # noqa: PLC0414 - pytest fixture registration
from tests.support.actions import enqueue, request_approved
from tests.support.actions import pg_action_platform as pg_action_platform  # noqa: PLC0414 - pytest fixture registration
from tests.support.actions import remote as remote  # noqa: PLC0414 - pytest fixture registration
from tests.support.platform import publish
from tests.support.postgres import pg_databases as pg_databases  # noqa: PLC0414 - pytest fixture registration
from tests.support.postgres import pg_platform as pg_platform  # noqa: PLC0414 - pytest fixture registration


def test_preview_idempotency_and_self_approval(action_platform):
    client, _app, a, b, payload, state = action_platform
    preview = client.post(
        "/api/actions/preview", json={k: v for k, v in payload.items() if k != "idempotency_key"}
    )
    assert preview.status_code == 200, preview.text
    assert preview.json()["snapshot"]["parameters"]["title"] == "跟进任务"
    assert state["post_calls"] == 0
    one = client.post("/api/actions", json=payload).json()
    two = client.post("/api/actions", json=payload).json()
    assert one["id"] == two["id"]
    changed = client.post(
        "/api/actions", json={**payload, "parameters": {**payload["parameters"], "title": "偷偷改参"}}
    )
    assert changed.status_code == 409
    assert (
        client.post(
            f"/api/actions/{one['id']}/approve", json={"expected_revision": one["revision"]}
        ).status_code
        == 403
    )
    assert (
        client.post(
            "/api/actions", json={**payload, "destination_url": "https://example.test/steal"}
        ).status_code
        == 422
    )
    assert client.get("/api/actions", params={"project_id": b["project_id"]}).status_code == 404
    assert client.get("/api/actions", params={"project_id": a["project_id"]}).json()["total"] == 1


def test_real_http_response_loss_requires_reconciliation(action_platform):
    client, app, a, _b, _payload, state = action_platform
    row = enqueue(action_platform)
    state["lose_response"] = True
    assert process_one_action(app.state.db, a["tenant_id"], app.state.action_adapters)
    unknown = client.get(f"/api/actions/{row['id']}").json()
    assert unknown["status"] == "unknown"
    assert unknown["result"]["receipt"] is None
    assert state["post_calls"] == 1 and len(state["receipts"]) == 1
    assert not process_one_action(app.state.db, a["tenant_id"], app.state.action_adapters)
    assert (
        client.post(
            f"/api/actions/{row['id']}/execute", json={"expected_revision": unknown["revision"]}
        ).status_code
        == 409
    )
    checked = client.post(
        f"/api/actions/{row['id']}/reconcile", json={"expected_revision": unknown["revision"]}
    )
    assert checked.status_code == 200, checked.text
    accepted = checked.json()
    assert accepted["status"] == "remote_accepted" and accepted["result"]["business_completed"] is False
    state["receipts"][row["id"]]["state"] = "completed"
    completed = client.post(
        f"/api/actions/{row['id']}/reconcile", json={"expected_revision": accepted["revision"]}
    )
    assert completed.json()["status"] == "completed"
    assert state["post_calls"] == 1 and state["get_calls"] == 2


@pytest.mark.parametrize("platform_fixture", ["action_platform", "pg_action_platform"])
def test_worker_entrypoint_sends_queued_action_and_persists_receipt(request, platform_fixture):
    platform = request.getfixturevalue(platform_fixture)
    client, app, a, _b, payload, state = platform
    queued = enqueue(platform)
    assert queued["status"] == "queued" and state["post_calls"] == 0

    # Use the production worker composition/tenant enumeration, with only the
    # fixed test destination injected. Never call claim/execute helpers here.
    run_worker(app.state.settings, once=True, action_registry=app.state.action_adapters)

    response = client.get(f"/api/actions/{queued['id']}")
    assert response.status_code == 200, response.text
    accepted = response.json()
    assert accepted["status"] == "remote_accepted"
    assert accepted["result"] == {
        "remote_idempotency_key": queued["id"],
        "receipt": "remote-" + queued["id"],
        "business_completed": False,
    }
    assert state["post_calls"] == 1 and len(state["receipts"]) == 1
    assert state["last_payload"]["parameters"]["title"] == payload["parameters"]["title"]
    with app.state.db.transaction(a["tenant_id"]) as session:
        box = session.scalar(select(ActionOutbox).where(ActionOutbox.run_id == queued["id"]))
        assert box.state == "remote_accepted" and box.attempts == 1 and box.lease_until == 0

    # A subsequent worker tick sees the persisted terminal send state and must
    # not create another remote task, including across Database instances.
    run_worker(app.state.settings, once=True, action_registry=app.state.action_adapters)
    assert state["post_calls"] == 1
    assert client.get(f"/api/actions/{queued['id']}").json()["result"] == accepted["result"]


def test_worker_entrypoint_without_registry_cannot_use_api_adapter(action_platform):
    client, app, a, _b, _payload, state = action_platform
    queued = enqueue(action_platform)
    # The API's in-process test registry is not ambient worker authorization.
    run_worker(app.state.settings, once=True)
    assert state["post_calls"] == 0 and not state["receipts"]
    result = client.get(f"/api/actions/{queued['id']}").json()
    assert result["status"] == "blocked"
    with app.state.db.transaction(a["tenant_id"]) as session:
        box = session.scalar(select(ActionOutbox).where(ActionOutbox.run_id == queued["id"]))
        assert box.attempts == 0


def test_changed_target_after_approval_blocks_before_send(action_platform):
    client, app, a, _b, payload, state = action_platform
    row = enqueue(action_platform)
    assert (
        client.patch(
            f"/api/objects/{payload['target_id']}",
            json={"expected_revision": 1, "properties": {"name": "目标已变化"}},
        ).status_code
        == 200
    )
    process_one_action(app.state.db, a["tenant_id"], app.state.action_adapters)
    blocked = client.get(f"/api/actions/{row['id']}").json()
    assert blocked["status"] == "blocked" and state["post_calls"] == 0


def test_requester_revoked_after_queue_blocks_worker(action_platform):
    _client, app, a, _b, _payload, state = action_platform
    row = enqueue(action_platform)
    with app.state.db.transaction(a["tenant_id"]) as session:
        session.get(User, a["user_id"]).active = False
    process_one_action(app.state.db, a["tenant_id"], app.state.action_adapters)
    with app.state.db.transaction(a["tenant_id"]) as session:
        assert session.get(ActionRun, row["id"]).status == "blocked"
    assert state["post_calls"] == 0


def test_leased_worker_recovery_and_cancel_fencing(action_platform):
    client, app, a, _b, _payload, state = action_platform
    row = enqueue(action_platform)
    db = app.state.db
    old = claim_action(db, a["tenant_id"], "old-worker")
    with db.transaction(a["tenant_id"]) as session:
        session.get(ActionOutbox, old["outbox_id"]).lease_until = time.time() - 1
    replacement = claim_action(db, a["tenant_id"], "new-worker")
    assert replacement["fencing_token"] > old["fencing_token"]
    with pytest.raises(DomainError):
        execute_claim(db, old, app.state.action_adapters)
    cancelled = client.post(f"/api/actions/{row['id']}/withdraw", json={"expected_revision": row["revision"]})
    assert cancelled.status_code == 200, cancelled.text
    with pytest.raises(DomainError):
        execute_claim(db, replacement, app.state.action_adapters)
    assert state["post_calls"] == 0


def test_crash_after_durable_send_marker_never_resends(action_platform):
    client, app, a, _b, _payload, state = action_platform
    row = enqueue(action_platform)
    db = app.state.db
    claimed = claim_action(db, a["tenant_id"], "crashed-worker")
    with db.transaction(a["tenant_id"]) as session:
        box = session.get(ActionOutbox, claimed["outbox_id"])
        box.state, box.lease_until = "sending", time.time() - 1
        session.get(ActionRun, row["id"]).status = "sending"
    assert claim_action(db, a["tenant_id"], "recovery-worker") is None
    unknown = client.get(f"/api/actions/{row['id']}").json()
    assert unknown["status"] == "unknown"
    result = client.post(
        f"/api/actions/{row['id']}/reconcile", json={"expected_revision": unknown["revision"]}
    )
    assert result.json()["status"] == "unknown"
    assert not process_one_action(db, a["tenant_id"], app.state.action_adapters)
    assert state["post_calls"] == 0


def test_default_feishu_action_does_not_send_without_configuration(action_platform):
    client, app, _a, _b, payload, state = action_platform
    app.state.action_adapters = {}
    row = request_approved(action_platform)
    response = client.post(f"/api/actions/{row['id']}/execute", json={"expected_revision": row["revision"]})
    assert response.status_code == 409 and response.json()["error"]["code"] == "action_adapter_unconfigured"
    assert state["post_calls"] == 0
    with app.state.db.transaction() as session:
        assert session.scalar(select(ActionOutbox.id)) is None
    assert (
        client.post(
            "/api/actions",
            json={
                **payload,
                "parameters": {**payload["parameters"], "assignee_id": "unknown"},
                "idempotency_key": "other-unique-key",
            },
        ).status_code
        == 422
    )


def test_approver_role_revoked_after_queue_blocks_send(action_platform):
    client, app, a, _b, payload, state = action_platform
    row = enqueue(action_platform)
    approver_id = payload["parameters"]["shared_user_ids"][0]
    changed = client.post(
        "/api/members", json={"project_id": a["project_id"], "user_id": approver_id, "role": "member"}
    )
    assert changed.status_code == 200
    process_one_action(app.state.db, a["tenant_id"], app.state.action_adapters)
    assert client.get(f"/api/actions/{row['id']}").json()["status"] == "blocked"
    assert state["post_calls"] == 0


def test_published_model_baseline_change_blocks_send(action_platform, definition):
    client, app, a, _b, _payload, state = action_platform
    row = enqueue(action_platform)
    publish(client, a["project_id"], definition)
    process_one_action(app.state.db, a["tenant_id"], app.state.action_adapters)
    assert client.get(f"/api/actions/{row['id']}").json()["status"] == "blocked"
    assert state["post_calls"] == 0


def test_postgres_concurrent_workers_send_once_and_tenant_outbox_isolated(pg_action_platform):
    client, app, a, b, _payload, state = pg_action_platform
    row = enqueue(pg_action_platform)
    db = app.state.db
    with db.transaction(b["tenant_id"]) as session:
        assert session.scalars(select(ActionOutbox)).all() == []
    with ThreadPoolExecutor(max_workers=2) as pool:
        claims = list(
            pool.map(lambda worker: claim_action(db, a["tenant_id"], worker), ["worker-a", "worker-b"])
        )
    actual = [claim for claim in claims if claim]
    assert len(actual) == 1
    execute_claim(db, actual[0], app.state.action_adapters)
    assert state["post_calls"] == 1
    result = client.get(f"/api/actions/{row['id']}").json()
    assert result["status"] == "remote_accepted" and not result["result"]["business_completed"]


def test_postgres_real_http_lost_response_and_approver_revocation(pg_action_platform):
    client, app, a, _b, payload, state = pg_action_platform
    row = enqueue(pg_action_platform)
    state["lose_response"] = True
    process_one_action(app.state.db, a["tenant_id"], app.state.action_adapters)
    unknown = client.get(f"/api/actions/{row['id']}").json()
    assert unknown["status"] == "unknown" and state["post_calls"] == 1
    recovered = client.post(
        f"/api/actions/{row['id']}/reconcile", json={"expected_revision": unknown["revision"]}
    )
    assert recovered.status_code == 200 and recovered.json()["status"] == "remote_accepted"
    payload["idempotency_key"] = "action-second-pg-key"
    second = enqueue(pg_action_platform)
    with app.state.db.transaction(a["tenant_id"]) as session:
        member = session.scalar(
            select(Membership).where(
                Membership.user_id == payload["parameters"]["shared_user_ids"][0],
                Membership.project_id == a["project_id"],
            )
        )
        member.role = "member"
    process_one_action(app.state.db, a["tenant_id"], app.state.action_adapters)
    assert client.get(f"/api/actions/{second['id']}").json()["status"] == "blocked"
    assert state["post_calls"] == 1
