"""The shared Source API must never impersonate an employee using stored credentials."""

import json

import pytest
from fastapi.testclient import TestClient
from ontology.connectors.http import SafeHTTPClient
from ontology.sources.models import Source
from ontology.sources.service import cipher


def source_payload(project_id, credential_kind="user"):
    return {
        "project_id": project_id,
        "name": "合成飞书来源边界测试",
        "kind": "feishu",
        "config": {
            "credential_kind": credential_kind,
            "resources": [{"resource_id": "doc", "kind": "docx", "resource_token": "document-resource-id"}],
        },
        "credentials": {"token": "synthetic-access-credential-never-return"},
    }


def forbid_remote_read(*args, **kwargs):
    pytest.fail("registering a source or rejecting an unbound identity must not read Feishu")


@pytest.mark.parametrize("credential_kind", ["user", "application"])
def test_resource_identifier_registers_locally_without_exposing_encrypted_credentials(
    platform, monkeypatch, credential_kind
):
    client, app, owner, _ = platform
    monkeypatch.setattr(SafeHTTPClient, "get_bytes", forbid_remote_read)
    payload = source_payload(owner["project_id"], credential_kind)
    response = client.post("/api/sources", json=payload)
    assert response.status_code == 201, response.text
    created = response.json()
    assert created["config"] == payload["config"]
    assert created["has_credentials"] is True and "secret" not in created
    assert payload["credentials"]["token"] not in response.text
    listing = client.get("/api/sources", params={"project_id": owner["project_id"]})
    assert listing.status_code == 200
    assert payload["credentials"]["token"] not in listing.text
    with app.state.db.transaction(owner["tenant_id"]) as session:
        stored = session.get(Source, created["id"])
        assert stored.secret and payload["credentials"]["token"] not in stored.secret
        assert (
            json.loads(cipher(app.state.settings).decrypt(stored.secret.encode())) == payload["credentials"]
        )
        assert stored.status == "unverified" and stored.resources == []


def test_nested_token_remains_rejected_and_is_not_a_resource_identifier_alias(platform):
    client, _, owner, _ = platform
    payload = source_payload(owner["project_id"])
    resource = payload["config"]["resources"][0]
    resource["token"] = "nested-credential-must-not-appear"
    response = client.post("/api/sources", json=payload)
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "credential_in_config"
    assert resource["token"] not in response.text
    del resource["resource_token"]
    legacy = client.post("/api/sources", json=payload)
    assert legacy.status_code == 422
    assert legacy.json()["error"]["code"] == "credential_in_config"
    assert client.get("/api/sources", params={"project_id": owner["project_id"]}).json()["total"] == 0


@pytest.mark.parametrize("credential_kind", ["user", "application"])
def test_shared_credentials_are_blocked_after_scope_checks_before_connector_or_state_changes(
    platform, monkeypatch, credential_kind
):
    client, app, owner, _ = platform
    monkeypatch.setattr(SafeHTTPClient, "get_bytes", forbid_remote_read)
    payload = source_payload(owner["project_id"], credential_kind)
    response = client.post("/api/sources", json=payload)
    assert response.status_code == 201, response.text
    source = response.json()
    person = client.post(
        "/api/users",
        json={
            "email": "source-maintainer@example.com",
            "display_name": "另一来源维护者",
            "password": "Synthetic-Maintainer-Password",
        },
    )
    assert person.status_code == 201, person.text
    member_response = client.post(
        "/api/members",
        json={"project_id": owner["project_id"], "user_id": person.json()["id"], "role": "modeler"},
    )
    assert member_response.status_code == 200, member_response.text

    def forbidden_connector(*args, **kwargs):
        pytest.fail("identity guard must precede connector construction and credential decryption")

    monkeypatch.setattr("ontology.sources.api.connector_for", forbidden_connector)
    monkeypatch.setattr("ontology.sources.api.cipher", forbidden_connector)
    with TestClient(app) as member, TestClient(app) as outsider:
        login = member.post(
            "/api/auth/login",
            json={
                "tenant_slug": "alpha",
                "email": "source-maintainer@example.com",
                "password": "Synthetic-Maintainer-Password",
            },
        )
        assert login.status_code == 200
        assert (
            "sources:write"
            in next(
                item
                for item in member.get("/api/auth/me").json()["projects"]
                if item["id"] == owner["project_id"]
            )["permissions"]
        )
        login = outsider.post(
            "/api/auth/login",
            json={
                "tenant_slug": "beta",
                "email": "owner@example.com",
                "password": "Safe-Test-Password-2026",
            },
        )
        assert login.status_code == 200
        for actor, expected_status in ((client, 422), (member, 422), (outsider, 404)):
            for operation in ("test", "discover", "sample"):
                path = f"/api/sources/{source['id']}/{operation}"
                result = (
                    actor.get(path, params={"resource": "doc"}) if operation == "sample" else actor.post(path)
                )
                assert result.status_code == expected_status, result.text
                if expected_status == 422:
                    assert result.json()["error"]["code"] == "employee_authorization_required"
                else:
                    assert "employee_authorization_required" not in result.text
                    assert "飞书" not in result.text and "feishu" not in result.text.lower()
                assert payload["credentials"]["token"] not in result.text

        revoked = client.post(
            "/api/members",
            json={"project_id": owner["project_id"], "user_id": person.json()["id"], "role": "member"},
        )
        assert revoked.status_code == 200
        for operation in ("test", "discover", "sample"):
            path = f"/api/sources/{source['id']}/{operation}"
            denied = (
                member.get(path, params={"resource": "doc"}) if operation == "sample" else member.post(path)
            )
            assert denied.status_code == 403
            assert "employee_authorization_required" not in denied.text

    current = client.get("/api/sources", params={"project_id": owner["project_id"]}).json()["items"][0]
    assert current == source
    with app.state.db.transaction(owner["tenant_id"]) as session:
        stored = session.get(Source, source["id"])
        assert stored.status == "unverified"
        assert stored.resources == [] and stored.last_error is None and stored.revision == 1
