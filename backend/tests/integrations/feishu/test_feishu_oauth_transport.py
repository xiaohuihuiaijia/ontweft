"""OAuth v3 over the actual pinned SDK and real loopback HTTP transport."""

import asyncio
import json
import threading
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

import pytest
from ontology.integrations.feishu.gateway import FeishuGatewayError
from ontology.integrations.feishu.oauth import READ_SCOPES, FeishuOAuth
from pydantic import SecretStr


@pytest.fixture
def oauth_remote(monkeypatch):
    monkeypatch.setenv("NO_PROXY", "127.0.0.1")
    monkeypatch.setenv("no_proxy", "127.0.0.1")
    received, replies = [], deque()

    class Handler(BaseHTTPRequestHandler):
        def handle_call(self):
            length = int(self.headers.get("Content-Length", "0"))
            body = self.rfile.read(length)
            received.append(
                {
                    "path": self.path,
                    "body": json.loads(body) if body else None,
                    "authorization": self.headers.get("Authorization"),
                }
            )
            response = replies.popleft()
            content = response.get("body", json.dumps(response.get("payload", {})).encode())
            self.send_response(response.get("status", 200))
            self.send_header("Content-Length", str(len(content)))
            self.send_header("Content-Type", "application/json")
            self.send_header("Location", "/must-not-follow")
            self.end_headers()
            self.wfile.write(content)

        do_GET = handle_call
        do_POST = handle_call

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=lambda: server.serve_forever(poll_interval=0.01), daemon=True)
    thread.start()
    oauth = FeishuOAuth("feishu", "cli_test", SecretStr("synthetic-client-secret"))
    origin = f"http://127.0.0.1:{server.server_port}"
    oauth._token_url = origin + "/oauth/v3/token"
    oauth._client._config.domain = origin
    yield SimpleNamespace(oauth=oauth, replies=replies, received=received)
    server.shutdown()
    server.server_close()
    thread.join(timeout=2)


def tokens(**changes):
    return {
        "code": 0,
        "access_token": "synthetic-user-access",
        "refresh_token": "synthetic-refresh",
        "token_type": "Bearer",
        "expires_in": 7200,
        "refresh_token_expires_in": 6000,
        "scope": " ".join(READ_SCOPES),
        **changes,
    }


def test_sdk_oauth_request_identity_and_refresh_contract(oauth_remote):
    remote = oauth_remote
    remote.replies.extend(
        [
            {"payload": tokens()},
            {
                "payload": {
                    "code": 0,
                    "data": {
                        "tenant_key": "tenant1",
                        "open_id": "open1",
                        "name": "员工",
                        "email": "must-not-collect",
                    },
                }
            },
            {"payload": tokens(access_token="rotated-access", refresh_token="rotated-refresh")},
        ]
    )
    obtained = asyncio.run(
        remote.oauth.exchange(SecretStr("code"), SecretStr("v" * 43), "https://our.example/callback")
    )
    identity = asyncio.run(remote.oauth.identity(obtained.access_token))
    refreshed = asyncio.run(remote.oauth.refresh(obtained.refresh_token))
    first, second, third = remote.received
    assert first["path"] == "/oauth/v3/token" and first["authorization"] is None
    assert first["body"]["code_verifier"] == "v" * 43
    assert first["body"]["redirect_uri"] == "https://our.example/callback"
    assert second["authorization"] == "Bearer synthetic-user-access"
    assert second["path"] == "/open-apis/authen/v1/user_info"
    assert identity.open_id == "open1" and not hasattr(identity, "email")
    assert third["body"]["grant_type"] == "refresh_token" and third["authorization"] is None
    assert refreshed.access_token.get_secret_value() == "rotated-access"
    assert "synthetic-user-access" not in repr(obtained)


@pytest.mark.parametrize(
    "response,expected",
    [
        ({"status": 307}, "redirect_rejected"),
        ({"status": 429, "body": b""}, "rate_limited"),
        ({"status": 502, "body": b"<html>"}, "upstream_unavailable"),
        (
            {"status": 400, "payload": {"code": 20073, "error_description": "synthetic-client-secret"}},
            "credential_invalid",
        ),
        ({"body": b'{"code": 0, "code": 1}'}, "invalid_response"),
        ({"payload": tokens(expires_in=True)}, "invalid_response"),
        ({"payload": tokens(scope="offline_access")}, "permission_denied"),
        ({"payload": tokens(refresh_token="contains\r\nnewline")}, "invalid_response"),
    ],
)
def test_oauth_errors_are_safe_and_never_followed_or_retried(oauth_remote, response, expected):
    oauth_remote.replies.append(response)
    with pytest.raises(FeishuGatewayError) as error:
        asyncio.run(
            oauth_remote.oauth.exchange(
                SecretStr("code"), SecretStr("v" * 43), "https://our.example/callback"
            )
        )
    assert error.value.code == expected
    assert "synthetic-client-secret" not in str(error.value)
    assert len(oauth_remote.received) == 1
