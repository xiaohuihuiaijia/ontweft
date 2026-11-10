"""Real SDK permission protocol plus scoped, non-persisting selection previews."""

import pytest
from ontology.integrations.feishu.gateway import DocumentMetadata, ResourcePermission, WikiNode
from ontology.integrations.feishu.service import FeishuUserGateway

from tests.support.feishu_employee import BASE, account, application, finish, start
from tests.support.feishu_employee import upstream as upstream  # noqa: PLC0414 - pytest fixture registration
from tests.support.feishu_sdk import remote as remote  # noqa: PLC0414 - pytest fixture registration


@pytest.fixture
def previews(platform, upstream, monkeypatch):
    return selection_fixture(platform, monkeypatch)


def selection_fixture(platform, monkeypatch):
    client, app, owner, _ = platform
    app_id = application(platform)
    assert finish(client, start(client, owner, app_id)["state"][0]).status_code == 200
    calls = []

    async def node(self, token):
        calls.append(("node", token))
        return WikiNode(
            self.context.reference(),
            "space",
            token,
            "docx",
            "actual-document",
            "shortcut" if token == "shortcut" else "origin",
            "origin",
            "space",
            False,
            "current-title",
        )

    async def permission(self, kind, token, *, action):
        calls.append((action, token))
        assert token == "actual-document"
        return ResourcePermission(self.context.reference(), kind, token, action, True)

    async def document(self, token):
        calls.append(("metadata", token))
        return DocumentMetadata(self.context.reference(), token, 17, "Current private document")

    monkeypatch.setattr(FeishuUserGateway, "get_wiki_node", node)
    monkeypatch.setattr(FeishuUserGateway, "check_permission", permission)
    monkeypatch.setattr(FeishuUserGateway, "get_document", document)

    def preview(nodes=("origin", "shortcut"), **changes):
        payload = {
            "project_id": owner["project_id"],
            "expected_account_revision": account(app, owner).revision,
            "selections": [{"space_id": "space", "node_token": token} for token in nodes],
            **changes,
        }
        return client.post(BASE + f"/applications/{app_id}/selection-preview", json=payload)

    return preview, calls
