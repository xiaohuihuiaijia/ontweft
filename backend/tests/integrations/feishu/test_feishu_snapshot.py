"""Actual SDK HTTP pagination and guards; loopback data, no real employee access."""

import asyncio
import json
from urllib.parse import parse_qs, urlsplit

import pytest
from ontology.documents import DocumentError, ParseLimits
from ontology.integrations.feishu.gateway import FeishuGatewayError
from ontology.integrations.feishu.snapshot import collect_docx

from tests.support.feishu_native import native_fixture
from tests.support.feishu_sdk import page
from tests.support.feishu_sdk import remote as sdk_remote_fixture

remote = sdk_remote_fixture


def reply(data):
    return {"payload": {"code": 0, "data": data}}


def queue(remote, *, second_revision=7, denied=False):
    value = native_fixture()
    metadata = {"document": {"document_id": "remote-doc", "revision_id": 7, "title": value["title"]}}
    remote.replies.extend(
        [
            reply({"auth_result": True}),
            reply({"auth_result": True}),
            reply(metadata),
            {"payload": page(*value["blocks"][:5], has_more=True, page_token="next")},
            {"payload": page(*value["blocks"][5:])},
            reply({"document": {**metadata["document"], "revision_id": second_revision}}),
            reply({"auth_result": not denied}),
            reply({"auth_result": True}),
        ]
    )
    return value


def test_real_sdk_collects_fixed_revision_and_preserves_original_blocks(remote):
    value = queue(remote)
    guarded = []

    async def guard():
        guarded.append(True)

    data = asyncio.run(collect_docx(remote.gateway(), "remote-doc", guard=guard))
    assert json.loads(data) == value and len(guarded) == 17
    reads = [r for r in remote.received if "/blocks?" in r["path"]]
    assert len(reads) == 2
    assert all(parse_qs(urlsplit(r["path"]).query)["document_revision_id"] == ["7"] for r in reads)
    assert all(r["authorization"] == "Bearer u-synthetic-a" for r in remote.received)
    assert b"u-synthetic-a" not in data and b"next_page_token" not in data


@pytest.mark.parametrize(
    "problem",
    [
        "revision",
        "denied",
        "late_revocation",
        "truncated",
        "duplicate",
        "cursor_cycle",
        "upstream_error",
        "page_budget",
        "byte_budget",
    ],
)
def test_snapshot_failure_never_returns_partial_content_or_changes_to_latest(remote, problem):
    value = queue(remote, second_revision=8 if problem == "revision" else 7, denied=problem == "denied")
    if problem == "truncated":
        remote.replies[4] = {"payload": page()}
    elif problem == "duplicate":
        remote.replies[4] = {"payload": page(value["blocks"][0])}
    elif problem == "cursor_cycle":
        remote.replies[4] = {"payload": page(*value["blocks"][5:], has_more=True, page_token="next")}
    elif problem == "upstream_error":
        remote.replies[4] = {"status": 429, "body": b"private-upstream-error"}

    async def guard():
        if problem == "late_revocation" and len(remote.received) >= 5:
            raise FeishuGatewayError("invalid_context")

    kwargs = (
        {"max_pages": 1}
        if problem == "page_budget"
        else {"limits": ParseLimits(max_bytes=100)}
        if problem == "byte_budget"
        else {}
    )
    with pytest.raises((DocumentError, FeishuGatewayError)) as error:
        asyncio.run(collect_docx(remote.gateway(), "remote-doc", guard=guard, **kwargs))
    assert "private-upstream-error" not in str(error.value)
    assert all("document_revision_id=-1" not in r["path"] for r in remote.received)


def test_collection_timeout_and_caller_cancellation_propagate(remote):
    queue(remote)

    async def slow_guard():
        await asyncio.sleep(1)

    with pytest.raises(DocumentError) as failure:
        asyncio.run(collect_docx(remote.gateway(), "remote-doc", guard=slow_guard, timeout_seconds=0.01))
    assert failure.value.code == "feishu_snapshot_timeout"
    assert not remote.received

    async def cancelled():
        raise asyncio.CancelledError()

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(collect_docx(remote.gateway(), "remote-doc", guard=cancelled))
    assert not remote.received
