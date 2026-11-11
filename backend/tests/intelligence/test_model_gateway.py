import json
from types import SimpleNamespace

import httpx
import pytest
from ontology.connectors.policy import NetworkPolicy
from ontology.core.errors import DomainError
from ontology.identity.models import Tenant, User
from ontology.intelligence.client import ModelCallError, ModelClient
from ontology.intelligence.connection_api import router
from ontology.intelligence.connections import connection_view, validate_connection
from ontology.intelligence.models import ModelConnection
from ontology.intelligence.schemas import ConnectionInput
from tests.support.postgres import pg_databases as pg_databases  # noqa: PLC0414 - fixture registration
from tests.support.postgres import pg_platform as pg_platform  # noqa: PLC0414 - fixture registration

SCHEMA = {
    "type": "object", "properties": {"name": {"type": "string"}},
    "required": ["name"], "additionalProperties": False,
}


@pytest.fixture
def model_client(monkeypatch):
    monkeypatch.setattr(NetworkPolicy, "check_host", lambda *args: None)
    connection = SimpleNamespace(
        enabled=True, secret="", allow_private_network=False, base_url="https://models.example.test/v1",
        roles={"extraction": "custom-model", "synthesis": "custom-model"}, models=[{
            "id": "custom-model", "vision": False, "json_schema": True,
            "context_window": 128000, "max_output_tokens": 2048,
            "input_price_per_million": 2, "output_price_per_million": 4,
        }],
    )
    return lambda callback: ModelClient(
        connection, SimpleNamespace(), transport=httpx.MockTransport(callback)
    )


def completion(content, *, finish="stop", usage=True):
    value = {"model": "resolved-model-123", "choices": [{"message": {"content": content}, "finish_reason": finish}]}
    if usage:
        value["usage"] = {"prompt_tokens": 100, "completion_tokens": 20}
    return httpx.Response(200, json=value)


def test_custom_compatible_model_and_normalized_usage(model_client):
    calls, reservations = [], []

    def respond(request):
        calls.append(json.loads(request.content))
        assert str(request.url) == "https://models.example.test/v1/chat/completions"
        return completion('{"name":"产品"}')

    result = model_client(respond).generate(
        "extraction", instruction="Extract name", text="产品", schema=SCHEMA,
        before_request=reservations.append,
    )
    assert result.data == {"name": "产品"}
    assert result.model == "resolved-model-123"
    assert result.usage["total_tokens"] == 120
    assert result.cost == 0.00028
    assert calls[0]["model"] == "custom-model"
    assert calls[0]["response_format"]["type"] == "json_schema"
    assert "tools" not in calls[0]
    assert reservations[0]["estimated_cost"] > result.cost


def test_one_schema_repair_is_counted_and_bounded(model_client):
    calls = []

    def respond(request):
        calls.append(json.loads(request.content))
        return completion('{"name":123}' if len(calls) == 1 else '{"name":"correct"}')

    result = model_client(respond).generate("extraction", instruction="Extract", text="x", schema=SCHEMA)
    assert len(calls) == result.requests == 2
    assert result.usage["total_tokens"] == 240
    assert len(calls[1]["messages"]) == 4
    assert result.cost == 0.00056


def test_second_invalid_result_stops_without_loop(model_client):
    calls = []

    def respond(request):
        calls.append(request)
        return completion('{"wrong":1}')

    with pytest.raises(ModelCallError) as caught:
        model_client(respond).generate("extraction", instruction="Extract", text="x", schema=SCHEMA)
    assert caught.value.code == "model_schema_invalid"
    assert caught.value.usage["total_tokens"] == 240
    assert len(calls) == 2


@pytest.mark.parametrize("status,code,retryable", [
    (401, "model_auth_failed", False), (403, "model_auth_failed", False),
    (429, "model_rate_limited", True), (503, "model_service_error", True),
    (400, "model_request_rejected", False), (302, "model_request_rejected", False),
])
def test_http_errors_are_redacted_and_not_retried(model_client, status, code, retryable):
    calls = []

    def respond(request):
        calls.append(request)
        return httpx.Response(status, text="sensitive provider error and synthetic credential")

    with pytest.raises(ModelCallError) as caught:
        model_client(respond).generate("extraction", instruction="Extract", text="x", schema=SCHEMA)
    assert caught.value.code == code
    assert caught.value.retryable == retryable
    assert "sensitive" not in str(caught.value)
    assert len(calls) == 1


def test_truncated_json_is_incomplete_not_repaired_or_accepted(model_client):
    with pytest.raises(ModelCallError) as caught:
        model_client(lambda _: completion('{"name":"x"}', finish="length")).generate(
            "extraction", instruction="Extract", text="x", schema=SCHEMA
        )
    assert caught.value.code == "model_output_truncated"


def test_missing_usage_is_not_claimed_free(model_client):
    result = model_client(lambda _: completion('{"name":"x"}', usage=False)).generate(
        "extraction", instruction="Extract", text="x", schema=SCHEMA
    )
    assert result.cost is None
    assert result.usage["reported"] is False


def test_partial_usage_does_not_undercharge_unknown_output(model_client):
    response = completion('{"name":"x"}').json()
    response["usage"] = {"prompt_tokens": 100}
    result = model_client(lambda _: httpx.Response(200, json=response)).generate(
        "extraction", instruction="Extract", text="x", schema=SCHEMA,
    )
    assert result.cost is None
    assert result.usage["reported"] is False


def test_thinking_is_bounded_and_reserved_separately_from_answer_tokens(model_client):
    calls, reservations = [], []

    def respond(request):
        calls.append(json.loads(request.content))
        return completion('{"name":"x"}')

    client = model_client(respond)
    client.connection.models[0]["parameters"] = {"enable_thinking": True, "thinking_budget": 4096}
    client.generate("synthesis", instruction="Extract", text="x", schema=SCHEMA, before_request=reservations.append)
    assert calls[0]["thinking_budget"] == 4096
    assert calls[0]["max_tokens"] == 2048
    assert reservations[0]["output_tokens"] == 6144
    assert reservations[0]["reasoning_tokens"] == 4096
    client.connection.models[0]["parameters"] = {"enable_thinking": True}
    with pytest.raises(DomainError) as caught:
        client.generate("synthesis", instruction="Extract", text="x", schema=SCHEMA)
    assert caught.value.code == "thinking_budget_required"
    assert len(calls) == 1


def test_generic_json_mode_and_vision_capability(model_client):
    client = model_client(lambda request: completion('{"name":"x"}'))
    client.connection.models[0]["json_schema"] = False
    assert client.generate("extraction", instruction="Extract", text="x", schema=SCHEMA).data["name"] == "x"
    with pytest.raises(DomainError) as caught:
        client.generate("extraction", instruction="Extract", text="x", images=["ignored"], schema=SCHEMA)
    assert caught.value.code == "model_vision_required"


def test_credentials_and_tool_parameters_cannot_hide_in_configuration():
    for extra in [
        {"base_url": "https://user:secret@example.test/v1"},
        {"base_url": "https://example.test/v1?api_key=secret"},
        {"models": [{"id": "test", "parameters": {"api_key": "secret"}}], "roles": {"extraction": "test", "synthesis": "test"}},
        {"models": [{"id": "test", "parameters": {"tools": []}}], "roles": {"extraction": "test", "synthesis": "test"}},
    ]:
        data = ConnectionInput.model_validate({"name": "Test", "base_url": "https://example.test/v1", **extra})
        with pytest.raises(DomainError):
            validate_connection(data)


@pytest.fixture(params=["platform", "pg_platform"])
def gateway_platform(request):
    client, app, a, b = request.getfixturevalue(request.param)
    if not any(getattr(route, "path", "") == "/api/model-connections" for route in app.routes):
        app.include_router(router, prefix="/api")
    return client, app, a, b


def test_admin_credentials_revision_and_enabled_user_catalog(gateway_platform):
    client, app, a, _b = gateway_platform
    body = {"name": "Compatible service", "base_url": "https://example.test/v1", "api_key": "synthetic-test-secret"}
    response = client.post("/api/model-connections", json=body)
    assert response.status_code == 201, response.text
    row = response.json()
    assert row["has_credentials"]
    assert "synthetic-test-secret" not in response.text
    with app.state.db.transaction(a["tenant_id"]) as session:
        stored = session.get(ModelConnection, row["id"])
        assert stored.secret and stored.secret != body["api_key"]
        assert "base_url" not in connection_view(stored)
    update = {**body, "expected_revision": row["revision"], "name": "Updated", "api_key": ""}
    assert client.put(f"/api/model-connections/{row['id']}", json=update).status_code == 200
    assert client.put(f"/api/model-connections/{row['id']}", json=update).status_code == 409
    with app.state.db.transaction(a["tenant_id"], write=True) as session:
        user = session.get(User, a["user_id"])
        user.admin = False
    public = client.get("/api/model-connections").json()["items"][0]
    assert "base_url" not in public and "has_credentials" not in public
    assert public["models"] and public["roles"]
    assert client.post("/api/model-connections", json=body).status_code == 403
    with app.state.db.transaction(a["tenant_id"], write=True) as session:
        session.get(ModelConnection, row["id"]).enabled = False
    assert client.get("/api/model-connections").json()["items"] == []


def test_model_connections_are_tenant_scoped(gateway_platform):
    client, app, _, other = gateway_platform
    body = {"name": "A", "base_url": "https://example.test/v1"}
    row = client.post("/api/model-connections", json=body).json()
    client.post("/api/auth/logout")
    with app.state.db.transaction(other["tenant_id"]) as session:
        tenant = session.get(Tenant, other["tenant_id"])
        user = session.get(User, other["user_id"])
        login = {"tenant_slug": tenant.slug, "email": user.email,
                 "password": "Postgres-Integration-Password" if app.state.db.engine.dialect.name == "postgresql" else "Safe-Test-Password-2026"}
    assert client.post("/api/auth/login", json=login).status_code == 200
    assert client.get("/api/model-connections").json()["items"] == []
    assert client.put(f"/api/model-connections/{row['id']}", json={**body, "expected_revision": 1}).status_code == 404


def test_malformed_configuration_never_echoes_secret(gateway_platform):
    client, *_ = gateway_platform
    response = client.post("/api/model-connections", json={"api_key": "synthetic-do-not-echo"})
    assert response.status_code == 422
    assert "synthetic-do-not-echo" not in response.text
