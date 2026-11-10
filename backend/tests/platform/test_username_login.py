"""Two-field login must retain tenant isolation, revocation and throttling."""

import pytest
from fastapi.testclient import TestClient
from ontology.api import create_app
from ontology.cli import bootstrap
from ontology.core.config import Settings
from ontology.identity.models import Tenant, User

from tests.support.postgres import pg_databases as pg_databases  # noqa: PLC0414
from tests.support.postgres import pg_platform as pg_platform  # noqa: PLC0414


@pytest.fixture(params=["platform", "pg_platform"])
def accounts(request):
    client, app, a, b = request.getfixturevalue(request.param)
    password = "Safe-Test-Password-2026" if request.param == "platform" else "Postgres-Integration-Password"
    for tenant in (a, b):
        with app.state.db.transaction(tenant["tenant_id"]) as session:
            session.get(User, tenant["user_id"]).username = "admin"
    with app.state.db.transaction() as session:
        slug = session.get(Tenant, a["tenant_id"]).slug
        other_slug = session.get(Tenant, b["tenant_id"]).slug
    app.state.settings.login_tenant_slug = slug
    return client, app, a, b, password, slug, other_slug


def test_username_email_and_legacy_login_resolve_same_account(accounts):
    _, app, a, b, password, slug, _ = accounts
    with TestClient(app) as client:
        for identifier in (
            {"username": "  AdMiN  "},
            {
                "username": " OWNER@EXAMPLE."
                + ("COM " if app.state.db.engine.dialect.name == "sqlite" else "TEST ")
            },
        ):
            response = client.post("/api/auth/login", json={**identifier, "password": password})
            assert response.status_code == 200, response.text
            assert response.json()["user"]["id"] == a["user_id"]
            assert "password_hash" not in response.text
            cookie = response.headers["set-cookie"].lower()
            assert "httponly" in cookie and "samesite=strict" in cookie
            assert client.get("/api/auth/me").json()["tenant"]["id"] == a["tenant_id"]
            assert b["project_id"] not in client.get("/api/projects").text
            assert client.post("/api/auth/logout").status_code == 200
            assert client.get("/api/auth/me").status_code == 401
        with app.state.db.transaction(a["tenant_id"]) as session:
            email = session.get(User, a["user_id"]).email
        response = client.post(
            "/api/auth/login", json={"tenant_slug": slug, "email": email, "password": password}
        )
        assert response.status_code == 200


def test_tenant_ambiguity_and_explicit_override_fail_closed(accounts):
    _, app, _, _, password, slug, other_slug = accounts
    with TestClient(app) as client:
        override = client.post(
            "/api/auth/login", json={"username": "admin", "password": password, "tenant_slug": other_slug}
        )
        assert override.status_code == 401
        app.state.settings.login_tenant_slug = None
        ambiguous = client.post("/api/auth/login", json={"username": "admin", "password": password})
        assert ambiguous.status_code == 401
        assert ambiguous.json() == override.json()
        assert client.get("/api/auth/me").status_code == 401
        app.state.settings.login_tenant_slug = "missing-enterprise"
        assert (
            client.post("/api/auth/login", json={"username": "admin", "password": password}).status_code
            == 401
        )
        app.state.settings.login_tenant_slug = slug
        assert (
            client.post("/api/auth/login", json={"username": "admin", "password": password}).status_code
            == 200
        )


def test_usernames_are_unique_per_enterprise_and_disabled_accounts_cannot_login(accounts):
    owner, app, _, _, _, _, _ = accounts
    payload = {
        "username": " Analyst ",
        "email": "analyst@example.test",
        "display_name": "分析员",
        "password": "Synthetic-Analyst-Password",
    }
    response = owner.post("/api/users", json=payload)
    assert response.status_code == 201, response.text
    member = response.json()
    assert member["username"] == "analyst"
    assert (
        owner.post(
            "/api/users", json={**payload, "email": "duplicate@example.test", "username": "ANALYST"}
        ).status_code
        == 409
    )
    assert owner.post("/api/users", json={**payload, "username": "admin@example.test"}).status_code == 422
    with TestClient(app) as client:
        login = {"username": "analyst", "password": payload["password"]}
        assert client.post("/api/auth/login", json=login).status_code == 200
        assert client.get("/api/auth/me").json()["projects"] == []
        assert client.post("/api/users", json={**payload, "username": "forbidden"}).status_code == 403
        assert owner.post(f"/api/users/{member['id']}/disable").status_code == 200
        assert client.get("/api/auth/me").status_code == 401
        assert client.post("/api/auth/login", json=login).status_code == 401


def test_failed_logins_are_generic_throttled_and_do_not_echo_passwords(accounts):
    _, app, _, _, _, _, _ = accounts
    password = "Synthetic-Wrong-Password"
    with TestClient(app) as client:
        unknown = client.post("/api/auth/login", json={"username": "unknown", "password": password})
        for i in range(10):
            response = client.post(
                "/api/auth/login", json={"username": " ADMIN " if i % 2 else "admin", "password": password}
            )
            assert response.status_code == 401
            assert response.json() == unknown.json()
        assert (
            client.post("/api/auth/login", json={"username": "admin", "password": password}).status_code
            == 429
        )
        assert client.get("/api/auth/me").status_code == 401
        malformed = client.post(
            "/api/auth/login",
            json={"username": "admin", "email": "private@example.test", "password": password},
        )
        assert malformed.status_code == 422
        assert password not in malformed.text and "private@example.test" not in malformed.text


def test_single_enterprise_can_resolve_login_without_configuration(tmp_path):
    app = create_app(
        Settings(
            database_url=f"sqlite:///{tmp_path / 'single.db'}", data_dir=tmp_path, login_tenant_slug=None
        )
    )
    owner = bootstrap(
        app.state.db,
        tenant_slug="solo",
        tenant_name="单企业",
        email="owner@example.test",
        username="Admin",
        password="Synthetic-Single-Password",
    )
    with TestClient(app) as client:
        response = client.post(
            "/api/auth/login", json={"username": "admin", "password": "Synthetic-Single-Password"}
        )
        assert response.status_code == 200
        assert response.json()["user"]["id"] == owner["user_id"]
