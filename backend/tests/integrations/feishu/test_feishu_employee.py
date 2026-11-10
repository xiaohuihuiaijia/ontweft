"""Employee lifecycle with deterministic upstream doubles; no real Feishu account."""

import time

import pytest
from fastapi.testclient import TestClient
from ontology.identity.models import LoginSession, Membership, User
from ontology.identity.security import token_hash
from ontology.integrations.feishu.gateway import FeishuGatewayError, SdkPage
from ontology.integrations.feishu.models import FeishuAccount, FeishuApplication, FeishuAuthorization
from ontology.integrations.feishu.oauth import EmployeeIdentity, FeishuOAuth
from ontology.integrations.feishu.service import FeishuUserGateway
from sqlalchemy import select

from tests.support.feishu_employee import BASE, account, application, browse, config, finish, start
from tests.support.feishu_employee import upstream as upstream  # noqa: PLC0414 - pytest fixture registration


def test_configuration_authorization_and_empty_page_lifecycle(platform, upstream):
    client, app, owner, _ = platform
    app_id = application(platform)
    listing = client.get(BASE + "/applications", params={"project_id": owner["project_id"]})
    assert listing.status_code == 200
    assert "synthetic-secret-never-echo" not in listing.text
    params = start(client, owner, app_id)
    assert params["code_challenge_method"] == ["S256"] and len(params["code_challenge"][0]) == 43
    state = params["state"][0]
    with app.state.db.transaction(owner["tenant_id"]) as session:
        attempt = session.get(FeishuAuthorization, token_hash(state))
        assert attempt and state not in attempt.verifier and len(attempt.verifier) > 86
        assert session.get(FeishuApplication, app_id).secret != config(owner["project_id"])["app_secret"]
    response = finish(client, state)
    assert response.status_code == 200, response.text
    assert finish(client, state).status_code == 409 and upstream["exchange"] == 1
    row = account(app, owner)
    assert row.status == "active" and "synthetic-access" not in row.secret
    result = browse(client, owner, app_id)
    assert result.status_code == 200, result.text
    assert result.json()["items"] == [] and result.json()["has_more"] is True
    assert "remote-private-page" not in result.text
    assert browse(client, owner, app_id, cursor=result.json()["next_cursor"]).status_code == 200
    with app.state.db.transaction(owner["tenant_id"]) as session:
        assert session.get(FeishuAuthorization, token_hash(state)).verifier == ""


@pytest.mark.parametrize(
    "boundary",
    [
        "other_tenant",
        "new_session",
        "restarted",
        "expired",
        "revoked_membership",
        "user_revision",
        "app_change",
    ],
)
def test_callback_does_not_cross_identity_or_state_generation(platform, upstream, boundary):
    client, app, owner, _ = platform
    app_id = application(platform)
    state = start(client, owner, app_id)["state"][0]
    if boundary in {"other_tenant", "new_session"}:
        client.post("/api/auth/logout", json={})
        client.post(
            "/api/auth/login",
            json={
                "tenant_slug": "beta" if boundary == "other_tenant" else "alpha",
                "email": "owner@example.com",
                "password": "Safe-Test-Password-2026",
            },
        )
    elif boundary == "restarted":
        assert start(client, owner, app_id)["state"][0] != state
    elif boundary == "app_change":
        assert (
            client.post(
                BASE + f"/applications/{app_id}", json=config(owner["project_id"], expected_revision=1)
            ).status_code
            == 200
        )
    else:
        with app.state.db.transaction(owner["tenant_id"]) as session:
            if boundary == "expired":
                session.get(FeishuAuthorization, token_hash(state)).expires_at = time.time() - 1
            elif boundary == "user_revision":
                session.get(User, owner["user_id"]).revision += 1
            else:
                session.delete(
                    session.scalar(select(Membership).where(Membership.user_id == owner["user_id"]))
                )
    result = finish(client, state)
    assert result.status_code in {401, 403, 404, 409}, result.text
    assert upstream["exchange"] == 0


def test_wrong_external_tenant_or_upstream_timeout_cannot_install_credentials(
    platform, upstream, monkeypatch
):
    client, app, owner, _ = platform
    app_id = application(platform)

    async def wrong(self, token):
        return EmployeeIdentity("wrong-tenant", "ou-other", "其他企业")

    monkeypatch.setattr(FeishuOAuth, "identity", wrong)
    state = start(client, owner, app_id)["state"][0]
    assert finish(client, state).status_code == 409
    assert account(app, owner).secret == ""

    async def timeout(self, *args):
        raise FeishuGatewayError("timed_out")

    monkeypatch.setattr(FeishuOAuth, "exchange", timeout)
    state = start(client, owner, app_id)["state"][0]
    assert finish(client, state).status_code == 422
    assert finish(client, state).status_code == 409
    assert account(app, owner).status == "reauthorize"


@pytest.mark.parametrize("change", ["disconnect", "app_change", "logout", "membership_revoked"])
def test_late_remote_response_is_discarded_after_revocation(platform, upstream, monkeypatch, change):
    client, app, owner, _ = platform
    app_id = application(platform)
    assert finish(client, start(client, owner, app_id)["state"][0]).status_code == 200
    session_hash = token_hash(client.cookies.get("ontology_session"))

    async def changed(self, **kwargs):
        with app.state.db.transaction(owner["tenant_id"]) as session:
            if change == "app_change":
                session.get(FeishuApplication, app_id).revision += 1
            elif change == "logout":
                session.delete(session.get(LoginSession, session_hash))
            elif change == "membership_revoked":
                session.delete(
                    session.scalar(select(Membership).where(Membership.user_id == owner["user_id"]))
                )
            else:
                row = session.get(FeishuAccount, account(app, owner).id)
                row.status = "revoked"
                row.revision += 1
        return SdkPage(
            self.context.reference(), ({"space_id": "s1", "name": "late-private-title"},), False, None
        )

    monkeypatch.setattr(FeishuUserGateway, "list_spaces", changed)
    response = browse(client, owner, app_id)
    assert response.status_code in {401, 403, 404, 409}, response.text
    assert "late-private-title" not in response.text


def test_refresh_rotates_once_and_old_cursors_are_invalid(platform, upstream):
    client, app, owner, _ = platform
    app_id = application(platform)
    finish(client, start(client, owner, app_id)["state"][0])
    cursor = browse(client, owner, app_id).json()["next_cursor"]
    with app.state.db.transaction(owner["tenant_id"]) as session:
        session.get(FeishuAccount, account(app, owner).id).expires_at = time.time() - 1
    assert browse(client, owner, app_id, cursor=cursor).status_code == 409
    assert upstream["refresh"] == 1
    assert browse(client, owner, app_id).status_code == 200
    assert upstream["refresh"] == 1


def test_cursor_cannot_move_between_projects_or_nodes(platform, upstream):
    client, _, owner, _ = platform
    app_id = application(platform)
    finish(client, start(client, owner, app_id)["state"][0])
    cursor = browse(client, owner, app_id).json()["next_cursor"]
    response = client.get(
        BASE + f"/applications/{app_id}/nodes",
        params={"project_id": owner["project_id"], "space_id": "s", "cursor": cursor},
    )
    assert response.status_code == 409
    assert browse(client, owner, app_id, cursor="tampered").status_code == 409
    assert upstream["browse"] == 1


def test_refresh_unknown_outcome_never_reuses_consumed_token(platform, upstream, monkeypatch):
    client, app, owner, _ = platform
    app_id = application(platform)
    finish(client, start(client, owner, app_id)["state"][0])
    with app.state.db.transaction(owner["tenant_id"]) as session:
        session.get(FeishuAccount, account(app, owner).id).expires_at = time.time() - 1

    async def failed(self, token):
        upstream["refresh"] += 1
        assert account(app, owner).secret == ""  # Reservation committed before network.
        raise FeishuGatewayError("timed_out")

    monkeypatch.setattr(FeishuOAuth, "refresh", failed)
    assert browse(client, owner, app_id).status_code == 422
    assert browse(client, owner, app_id).status_code == 409
    assert upstream["refresh"] == 1


def test_validation_and_denial_do_not_echo_secrets(platform, upstream):
    client, app, owner, _ = platform
    invalid = client.post(
        BASE + "/applications", json=config(owner["project_id"], app_id="invalid id", unexpected="sensitive")
    )
    assert invalid.status_code == 422
    assert "sensitive" not in invalid.text and "synthetic-secret" not in invalid.text
    app_id = application(platform)
    state = start(client, owner, app_id)["state"][0]
    denied = client.post(BASE + "/callback", json={"state": state, "error": "access_denied"})
    assert denied.status_code == 409 and upstream["exchange"] == 0
    assert account(app, owner).status == "reauthorize"


def test_non_admin_can_only_connect_own_account_and_other_tenant_hidden(platform, upstream):
    client, app, owner, other = platform
    app_id = application(platform)
    with app.state.db.transaction(owner["tenant_id"]) as session:
        session.get(User, owner["user_id"]).admin = False
    assert (
        client.post(BASE + "/applications", json=config(owner["project_id"], app_id="another")).status_code
        == 403
    )
    assert finish(client, start(client, owner, app_id)["state"][0]).status_code == 200
    with TestClient(app) as second:
        second.post(
            "/api/auth/login",
            json={"tenant_slug": "beta", "email": "owner@example.com", "password": "Safe-Test-Password-2026"},
        )
        assert (
            second.get(BASE + "/applications", params={"project_id": other["project_id"]}).json()["items"]
            == []
        )
        assert (
            second.post(
                BASE + f"/applications/{app_id}/authorize", json={"project_id": other["project_id"]}
            ).status_code
            == 404
        )


def test_same_enterprise_employee_cannot_borrow_another_employees_binding(platform, upstream):
    client, app, owner, _ = platform
    app_id = application(platform)
    assert finish(client, start(client, owner, app_id)["state"][0]).status_code == 200
    with app.state.db.transaction(owner["tenant_id"]) as session:
        original = session.get(User, owner["user_id"])
        colleague = User(
            tenant_id=owner["tenant_id"],
            email="colleague@example.com",
            display_name="同企业员工",
            password_hash=original.password_hash,
            admin=False,
        )
        session.add(colleague)
        session.flush()
        session.add(
            Membership(
                tenant_id=owner["tenant_id"],
                project_id=owner["project_id"],
                user_id=colleague.id,
                role="modeler",
            )
        )
    with TestClient(app) as second:
        result = second.post(
            "/api/auth/login",
            json={
                "tenant_slug": "alpha",
                "email": "colleague@example.com",
                "password": "Safe-Test-Password-2026",
            },
        )
        assert result.status_code == 200
        listing = second.get(BASE + "/applications", params={"project_id": owner["project_id"]})
        assert listing.json()["items"][0]["account"] is None
        assert "合成员工" not in listing.text
        assert browse(second, owner, app_id).status_code == 409
        assert upstream["browse"] == 0


def test_inflight_callback_cannot_restore_a_disconnected_binding(platform, upstream, monkeypatch):
    client, app, owner, _ = platform
    app_id = application(platform)
    state = start(client, owner, app_id)["state"][0]
    original = FeishuOAuth.identity

    async def disconnected(self, token):
        with app.state.db.transaction(owner["tenant_id"]) as session:
            row = session.get(FeishuAccount, account(app, owner).id)
            row.status, row.secret = "revoked", ""
            row.revision += 1
        return await original(self, token)

    monkeypatch.setattr(FeishuOAuth, "identity", disconnected)
    assert finish(client, state).status_code == 409
    row = account(app, owner)
    assert row.status == "revoked" and row.secret == ""
