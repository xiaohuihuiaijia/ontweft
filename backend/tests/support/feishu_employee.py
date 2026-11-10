"""Employee lifecycle with deterministic upstream doubles; no real Feishu account."""

from urllib.parse import parse_qs, urlsplit

import pytest
from ontology.integrations.feishu.gateway import SdkPage
from ontology.integrations.feishu.models import FeishuAccount
from ontology.integrations.feishu.oauth import READ_SCOPES, EmployeeIdentity, FeishuOAuth, OAuthTokens
from ontology.integrations.feishu.service import FeishuUserGateway
from pydantic import SecretStr
from sqlalchemy import select

BASE = "/api/integrations/feishu"


def config(project_id, **changes):
    return {
        "project_id": project_id,
        "name": "企业知识",
        "app_id": "cli_synthetic",
        "service": "feishu",
        "external_tenant_key": "tenant-synthetic",
        "app_secret": "synthetic-secret-never-echo",
        **changes,
    }


def application(platform):
    client, _, owner, _ = platform
    response = client.post(BASE + "/applications", json=config(owner["project_id"]))
    assert response.status_code == 201, response.text
    return response.json()["id"]


def start(client, owner, app_id):
    result = client.post(BASE + f"/applications/{app_id}/authorize", json={"project_id": owner["project_id"]})
    assert result.status_code == 200, result.text
    return parse_qs(urlsplit(result.json()["authorization_url"]).query)


def finish(client, state):
    return client.post(BASE + "/callback", json={"state": state, "code": "synthetic-code-never-echo"})


@pytest.fixture
def upstream(monkeypatch):
    calls = {"exchange": 0, "refresh": 0, "identity": 0, "browse": 0}

    def tokens():
        return OAuthTokens(
            SecretStr("synthetic-access-private"),
            SecretStr("synthetic-refresh-private"),
            7200,
            604800,
            READ_SCOPES,
        )

    async def exchange(self, code, verifier, redirect):
        assert len(verifier.get_secret_value()) >= 43
        assert redirect == "http://127.0.0.1:5173/integrations/feishu/callback"
        calls["exchange"] += 1
        return tokens()

    async def refresh(self, token):
        assert token.get_secret_value() == "synthetic-refresh-private"
        calls["refresh"] += 1
        return tokens()

    async def identity(self, token):
        calls["identity"] += 1
        return EmployeeIdentity("tenant-synthetic", "ou-employee", "合成员工")

    async def spaces(self, **kwargs):
        calls["browse"] += 1
        return SdkPage(self.context.reference(), (), True, "remote-private-page")

    monkeypatch.setattr(FeishuOAuth, "exchange", exchange)
    monkeypatch.setattr(FeishuOAuth, "refresh", refresh)
    monkeypatch.setattr(FeishuOAuth, "identity", identity)
    monkeypatch.setattr(FeishuUserGateway, "list_spaces", spaces)
    return calls


def account(app, owner):
    with app.state.db.transaction(owner["tenant_id"]) as session:
        return session.scalar(select(FeishuAccount).where(FeishuAccount.tenant_id == owner["tenant_id"]))


def browse(client, owner, app_id, **params):
    return client.get(
        BASE + f"/applications/{app_id}/spaces", params={"project_id": owner["project_id"], **params}
    )
