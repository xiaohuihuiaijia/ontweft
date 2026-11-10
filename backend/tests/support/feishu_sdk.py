"""Real pinned SDK over a loopback HTTP server; never a user's Feishu account."""

import json
import threading
import time
from collections import deque
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

import pytest
from ontology.integrations.feishu import EmployeeBindingContext, FeishuUserGateway
from pydantic import SecretStr


def binding(**changes):
    return replace(
        EmployeeBindingContext(
            platform_tenant_id="platform-a",
            user_id="employee-a",
            connection_id="connection-a",
            external_tenant_key="remote-a",
            external_user_id="open-a",
            credential_revision=2,
            user_access_token=SecretStr("u-synthetic-a"),
            expires_at=datetime.now(UTC) + timedelta(hours=1),
        ),
        **changes,
    )


@pytest.fixture
def remote(monkeypatch):
    # SDK owns its httpx client. Local tests bypass operator proxy configuration
    # solely for loopback; production transport behavior is not monkeypatched.
    monkeypatch.setenv("NO_PROXY", "127.0.0.1")
    monkeypatch.setenv("no_proxy", "127.0.0.1")
    replies, received = deque(), []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            received.append({"path": self.path, "authorization": self.headers.get("Authorization")})
            response = replies.popleft() if replies else {"status": 500, "payload": {"code": 999}}
            time.sleep(response.get("delay", 0))
            content = response.get("body")
            if content is None:
                content = json.dumps(response.get("payload"), ensure_ascii=False).encode()
            self.send_response(response.get("status", 200))
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(content)))
            for name, value in response.get("headers", {}).items():
                self.send_header(name, value)
            self.end_headers()
            try:
                self.wfile.write(content)
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                # Timeout tests deliberately close the client before the late reply.
                pass

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=lambda: server.serve_forever(poll_interval=0.01), daemon=True)
    thread.start()

    def gateway(context=None, **kwargs):
        result = FeishuUserGateway(context or binding(), **kwargs)
        # Only this test instance changes domain. All generated request objects,
        # SDK auth selection/serialization and actual HTTP transport remain real.
        result._client._config.domain = f"http://127.0.0.1:{server.server_port}"
        return result

    yield SimpleNamespace(replies=replies, received=received, gateway=gateway)
    server.shutdown()
    server.server_close()
    thread.join(timeout=2)


def page(*items, has_more=False, page_token=None):
    return {"code": 0, "data": {"items": list(items), "has_more": has_more, "page_token": page_token}}
