"""Action tests use a real loopback HTTP server, never a live Feishu account."""

import json
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from fastapi.testclient import TestClient
from ontology.actions.api import router
from ontology.actions.runtime import (
    ACTION_KEY,
    LoopbackTaskAdapter,
)

from tests.support.platform import publish
from tests.support.postgres import pg_databases as pg_databases  # noqa: PLC0414 - pytest fixture registration
from tests.support.postgres import pg_platform as pg_platform  # noqa: PLC0414 - pytest fixture registration


@pytest.fixture
def remote():
    state = {"receipts": {}, "post_calls": 0, "get_calls": 0, "lose_response": False}
    lock = threading.Lock()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_POST(self):
            assert self.path == "/tasks"
            payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            key = self.headers["Idempotency-Key"]
            with lock:
                state["post_calls"] += 1
                receipt = state["receipts"].setdefault(
                    key, {"state": "remote_accepted", "receipt": "remote-" + key}
                )
                state["last_payload"] = payload
                lose = state["lose_response"]
            if lose:
                self.connection.shutdown(socket.SHUT_RDWR)
                self.connection.close()
                return
            self.respond(200, receipt)

        def do_GET(self):
            key = self.path.rsplit("/", 1)[-1]
            with lock:
                state["get_calls"] += 1
                receipt = state["receipts"].get(key)
            self.respond(200 if receipt else 404, receipt or {})

        def respond(self, code, body):
            data = json.dumps(body).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield state, LoopbackTaskAdapter(f"http://127.0.0.1:{server.server_port}", timeout=1)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)


@pytest.fixture
def action_platform(platform, definition, remote):
    return configure_platform(platform, definition, remote)


@pytest.fixture
def pg_action_platform(request, definition, remote):
    return configure_platform(request.getfixturevalue("pg_platform"), definition, remote)


def configure_platform(platform, definition, remote):
    client, app, a, b = platform
    if "/api/actions/preview" not in app.openapi()["paths"]:
        app.include_router(router, prefix="/api")
        app.openapi_schema = None
    app.state.action_adapters = {ACTION_KEY: remote[1]}
    app.state.db.create_schema()
    publish(client, a["project_id"], definition)
    obj = client.post(
        "/api/objects",
        json={
            "project_id": a["project_id"],
            "type_key": "Project",
            "properties": {"name": "待办来源", "status": "open"},
        },
    ).json()
    reviewer = client.post(
        "/api/users",
        json={
            "email": "reviewer@example.test",
            "display_name": "审核员",
            "password": "Action-Reviewer-Password",
        },
    ).json()
    client.post(
        "/api/members", json={"project_id": a["project_id"], "user_id": reviewer["id"], "role": "reviewer"}
    )
    payload = {
        "project_id": a["project_id"],
        "target_id": obj["id"],
        "expected_target_revision": 1,
        "action_key": ACTION_KEY,
        "idempotency_key": "action-fixture-001",
        "parameters": {
            "title": "跟进任务",
            "body": "请审核来源事项",
            "assignee_id": a["user_id"],
            "shared_user_ids": [reviewer["id"]],
        },
    }
    return client, app, a, b, payload, remote[0]


def request_approved(p):
    client, app, _a, _b, payload, _remote = p
    response = client.post("/api/actions", json=payload)
    assert response.status_code == 201, response.text
    row = response.json()
    tenant_slug = client.get("/api/auth/me").json()["tenant"]["slug"]
    with TestClient(app) as reviewer:
        assert (
            reviewer.post(
                "/api/auth/login",
                json={
                    "tenant_slug": tenant_slug,
                    "email": "reviewer@example.test",
                    "password": "Action-Reviewer-Password",
                },
            ).status_code
            == 200
        )
        approved = reviewer.post(
            f"/api/actions/{row['id']}/approve", json={"expected_revision": row["revision"]}
        )
        assert approved.status_code == 200, approved.text
    return approved.json()


def enqueue(p):
    row = request_approved(p)
    result = p[0].post(f"/api/actions/{row['id']}/execute", json={"expected_revision": row["revision"]})
    assert result.status_code == 200, result.text
    return result.json()
