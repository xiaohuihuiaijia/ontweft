"""Real SDK permission protocol plus scoped, non-persisting selection previews."""

import asyncio
from dataclasses import asdict, replace
from urllib.parse import parse_qs, urlsplit

import pytest
from ontology.documents.storage_models import Document
from ontology.integrations.feishu.gateway import (
    DocumentMetadata,
    FeishuGatewayError,
    ResourcePermission,
)
from ontology.integrations.feishu.models import FeishuAccount
from ontology.integrations.feishu.selection import _gather
from ontology.integrations.feishu.service import FeishuUserGateway
from ontology.jobs.models import Job
from sqlalchemy import func, select

from tests.support.feishu_employee import account
from tests.support.feishu_employee import upstream as upstream  # noqa: PLC0414 - pytest fixture registration
from tests.support.feishu_sdk import remote as remote  # noqa: PLC0414 - pytest fixture registration
from tests.support.feishu_selection import previews as previews  # noqa: PLC0414 - pytest fixture registration


def test_sdk_checks_resource_action_with_employee_identity_and_preserves_false(remote):
    gateway = remote.gateway()
    remote.replies.extend(
        [
            {"payload": {"code": 0, "data": {"auth_result": True}}},
            {"payload": {"code": 0, "data": {"auth_result": False}}},
        ]
    )
    view = asyncio.run(gateway.check_permission("docx", "actual/document?token", action="view"))
    copy = asyncio.run(gateway.check_permission("docx", "actual/document?token", action="copy"))
    assert view.allowed is True and copy.allowed is False
    assert copy.resource_token == "actual/document?token" and copy.action == "copy"
    assert all(item["authorization"] == "Bearer u-synthetic-a" for item in remote.received)
    assert remote.received[0]["path"].startswith(
        "/open-apis/drive/v1/permissions/actual%2Fdocument%3Ftoken/members/auth?"
    )
    assert parse_qs(urlsplit(remote.received[1]["path"]).query) == {"type": ["docx"], "action": ["copy"]}
    assert "u-synthetic-a" not in str(asdict(copy))


@pytest.mark.parametrize("value", [None, "true", "false", 1, 0, [], {}])
def test_sdk_does_not_coerce_unknown_permission_into_boolean(remote, value):
    remote.replies.append({"payload": {"code": 0, "data": {"auth_result": value}}})
    with pytest.raises(FeishuGatewayError, match="incomplete"):
        asyncio.run(remote.gateway().check_permission("docx", "doc", action="export"))


def test_wiki_resolution_keeps_location_distinct_from_actual_resource(remote):
    remote.replies.append(
        {
            "payload": {
                "code": 0,
                "data": {
                    "node": {
                        "node_token": "shortcut",
                        "space_id": "9223372036854775807",
                        "obj_type": "docx",
                        "obj_token": "actual-document",
                        "node_type": "shortcut",
                        "origin_node_token": "origin",
                        "origin_space_id": "original-space",
                        "has_child": False,
                        "title": "示例",
                    }
                },
            }
        }
    )
    node = asyncio.run(remote.gateway().get_wiki_node("shortcut"))
    assert node.node_token == "shortcut" and node.resource_token == "actual-document"
    assert node.space_id == "9223372036854775807"
    assert parse_qs(urlsplit(remote.received[0]["path"]).query) == {
        "token": ["shortcut"],
        "obj_type": ["wiki"],
    }


def test_selection_deduplicates_shortcuts_without_writing_documents_or_jobs(platform, previews):
    preview, calls = previews
    result = preview()
    assert result.status_code == 200, result.text
    data = result.json()
    assert data["eligible_unique_resources"] == 1 and data["selected_nodes"] == 2
    assert data["documents_written"] == 0 and data["scope"] == "selected_nodes_only"
    assert {item["revision_id"] for item in data["items"]} == {17}
    assert calls.count(("view", "actual-document")) == calls.count(("copy", "actual-document")) == 1
    assert calls.count(("metadata", "actual-document")) == 1
    _, app, owner, _ = platform
    with app.state.db.transaction(owner["tenant_id"]) as session:
        for model in (Document, Job):
            assert session.scalar(select(func.count()).select_from(model)) == 0


@pytest.mark.parametrize("operation", ["view", "copy"])
def test_denied_permission_never_returns_title_or_fetches_content(previews, monkeypatch, operation):
    preview, calls = previews

    async def permission(self, kind, token, *, action):
        calls.append((action, token))
        return ResourcePermission(self.context.reference(), kind, token, action, action != operation)

    monkeypatch.setattr(FeishuUserGateway, "check_permission", permission)
    result = preview()
    assert result.status_code == 200
    assert {row["status"] for row in result.json()["items"]} == {"denied"}
    assert "Current private" not in result.text and "actual-document" not in result.text
    assert ("metadata", "actual-document") not in calls
    if operation == "view":
        assert ("copy", "actual-document") not in calls


def test_transport_failure_is_unknown_instead_of_denial_or_deletion(previews, monkeypatch):
    preview, calls = previews

    async def unavailable(self, *args, **kwargs):
        raise FeishuGatewayError("rate_limited", retryable=True)

    monkeypatch.setattr(FeishuUserGateway, "check_permission", unavailable)
    result = preview()
    assert result.status_code == 200
    assert all(
        row["status"] == "unknown" and row["error_code"] == "rate_limited" for row in result.json()["items"]
    )
    assert ("metadata", "actual-document") not in calls


@pytest.mark.parametrize("method", ["get_wiki_node", "get_document"])
def test_explicit_upstream_permission_denial_does_not_become_unknown(previews, monkeypatch, method):
    preview, _ = previews

    async def denied(self, *args, **kwargs):
        raise FeishuGatewayError("permission_denied")

    monkeypatch.setattr(FeishuUserGateway, method, denied)
    result = preview()
    assert result.status_code == 200
    assert {row["status"] for row in result.json()["items"]} == {"denied"}
    assert "actual-document" not in result.text and "Current private" not in result.text


def test_non_docx_permission_success_does_not_claim_content_support(previews, monkeypatch):
    preview, calls = previews
    original = FeishuUserGateway.get_wiki_node

    async def file_node(self, token):
        return replace(await original(self, token), resource_type="file")

    monkeypatch.setattr(FeishuUserGateway, "get_wiki_node", file_node)
    result = preview()
    assert result.status_code == 200
    assert {row["status"] for row in result.json()["items"]} == {"format_pending"}
    assert result.json()["eligible_unique_resources"] == 0
    assert "actual-document" not in result.text and ("metadata", "actual-document") not in calls


def test_fatal_parallel_read_cancels_and_awaits_outstanding_sibling():
    async def scenario():
        entered, cancelled = asyncio.Event(), asyncio.Event()

        async def fatal():
            await entered.wait()
            raise FeishuGatewayError("credential_invalid")

        async def pending():
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

        with pytest.raises(FeishuGatewayError) as error:
            await _gather([fatal(), pending()])
        assert error.value.code == "credential_invalid"
        assert cancelled.is_set()

    asyncio.run(scenario())


def test_moved_nodes_do_not_read_the_replacement_resource(previews):
    preview, calls = previews
    result = preview(selections=[{"space_id": "old-space", "node_token": "origin"}])
    assert result.json()["items"][0]["status"] == "location_changed"
    assert calls == [("node", "origin")]


def test_stale_duplicate_and_oversized_selection_rejected_before_remote_reads(previews):
    preview, calls = previews
    assert preview(expected_account_revision=999).status_code == 409
    assert preview(nodes=("origin", "origin")).status_code == 422
    assert preview(nodes=tuple(str(i) for i in range(21))).status_code == 422
    assert calls == []


def test_late_preview_is_rejected_after_binding_revocation(platform, previews, monkeypatch):
    preview, _ = previews
    _, app, owner, _ = platform

    async def revoke(self, token):
        with app.state.db.transaction(owner["tenant_id"]) as session:
            row = session.get(FeishuAccount, account(app, owner).id)
            row.revision += 1
            row.status = "revoked"
            row.secret = ""
        return DocumentMetadata(self.context.reference(), token, 17, "Late private title")

    monkeypatch.setattr(FeishuUserGateway, "get_document", revoke)
    result = preview()
    assert result.status_code == 409 and "Late private title" not in result.text


def test_old_scope_binding_requires_reauthorization_before_preview(platform, previews):
    preview, calls = previews
    _, app, owner, _ = platform
    with app.state.db.transaction(owner["tenant_id"]) as session:
        row = session.get(FeishuAccount, account(app, owner).id)
        row.scopes = [scope for scope in row.scopes if scope != "docs:permission.member:auth"]
    assert preview().status_code == 409
    assert calls == []
