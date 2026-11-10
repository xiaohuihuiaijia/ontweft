"""Real pinned SDK over a loopback HTTP server; never a user's Feishu account."""

import asyncio
import json
import time
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs, urlsplit

import pytest
from lark_oapi.api.wiki.v2 import ListSpaceRequest
from lark_oapi.core.enum import AccessTokenType
from lark_oapi.core.token.manager import TokenManager
from ontology.integrations.feishu import FeishuGatewayError, FeishuUserGateway
from pydantic import SecretStr

from tests.support.feishu_sdk import binding, page
from tests.support.feishu_sdk import remote as remote  # noqa: PLC0414 - pytest fixture registration


def test_real_sdk_pages_use_fresh_user_requests_and_preserve_raw_nodes(remote):
    original_cache = TokenManager.cache
    gateway = remote.gateway()
    assert TokenManager.cache is original_cache
    assert gateway._client._config.app_secret is None
    assert gateway._client._config.app_id is None
    assert gateway._client._config.cache is None
    observed_requests = []
    original_call = gateway._call

    async def observe(request):
        if isinstance(request, ListSpaceRequest):
            observed_requests.append(request)
        result = await original_call(request)
        assert request.token_types == {AccessTokenType.USER}
        return result

    gateway._call = observe
    remote.replies.extend(
        [
            {"payload": page({"space_id": "s1", "name": "研发空间 🧪"}, has_more=True, page_token="p2")},
            {"payload": page({"space_id": "s2", "name": "项目空间"})},
            {
                "payload": page(
                    {
                        "node_token": "n1",
                        "obj_token": "doc1",
                        "obj_type": "docx",
                        "origin_node_token": "origin",
                        "unknown_future": {"keep": True},
                    }
                )
            },
        ]
    )
    first = asyncio.run(gateway.list_spaces(page_size=1))
    assert first.has_more and first.next_page_token == "p2"
    assert len(remote.received) == 1  # No automatic next page or fabricated total.
    assert not hasattr(first, "total")
    second = asyncio.run(gateway.list_spaces(page_size=1, page_token=first.next_page_token))
    nodes = asyncio.run(gateway.list_nodes("space/with?reserved", parent_node_token="parent", page_size=1))
    assert not second.has_more and second.next_page_token is None
    assert observed_requests[0] is not observed_requests[1]
    assert nodes.items[0]["unknown_future"] == {"keep": True}
    assert nodes.items[0]["origin_node_token"] == "origin"
    assert "space%2Fwith%3Freserved/nodes" in remote.received[2]["path"]
    assert parse_qs(urlsplit(remote.received[2]["path"]).query) == {
        "page_size": ["1"],
        "parent_node_token": ["parent"],
    }
    assert all(item["authorization"] == "Bearer u-synthetic-a" for item in remote.received)
    assert "u-synthetic-a" not in repr(gateway.context)
    result_json = json.dumps(asdict(first), ensure_ascii=False)
    assert "user_access_token" not in result_json and "u-synthetic-a" not in result_json


def test_two_employee_clients_do_not_share_credentials_or_result_scope(remote):
    other = binding(
        platform_tenant_id="platform-b",
        external_tenant_key="remote-b",
        user_id="employee-b",
        user_access_token=SecretStr("u-synthetic-b"),
    )
    gateways = [remote.gateway(binding()), remote.gateway(other)]
    remote.replies.extend([{"payload": page({"space_id": "same-id"})}] * 2)

    async def both():
        return await asyncio.gather(*(gateway.list_spaces() for gateway in gateways))

    results = asyncio.run(both())
    assert {item["authorization"] for item in remote.received} == {
        "Bearer u-synthetic-a",
        "Bearer u-synthetic-b",
    }
    assert [result.binding.platform_tenant_id for result in results] == ["platform-a", "platform-b"]
    assert [result.binding.user_id for result in results] == ["employee-a", "employee-b"]


def test_docx_metadata_and_fixed_revision_blocks_keep_rich_structure(remote):
    gateway = remote.gateway()
    block = {
        "block_id": "b1",
        "block_type": 2,
        "parent_id": "doc1",
        "text": {"elements": [{"text_run": {"content": "中文内容", "text_element_style": {"bold": True}}}]},
    }
    remote.replies.extend(
        [
            {
                "payload": {
                    "code": 0,
                    "data": {
                        "document": {"document_id": "doc1", "revision_id": 7, "title": "跨企业项目 — 🧪"}
                    },
                }
            },
            {"payload": page(block, has_more=True, page_token="next")},
            {"payload": page()},
        ]
    )
    metadata = asyncio.run(gateway.get_document("doc1"))
    assert metadata.title == "跨企业项目 — 🧪"
    first = asyncio.run(gateway.list_document_blocks("doc1", revision_id=metadata.revision_id, page_size=1))
    assert first.items == (block,) and first.document_revision_id == 7
    asyncio.run(gateway.list_document_blocks("doc1", revision_id=7, page_size=1, page_token="next"))
    for received in remote.received[1:]:
        query = parse_qs(urlsplit(received["path"]).query)
        assert query["document_revision_id"] == ["7"] and query["user_id_type"] == ["open_id"]


@pytest.mark.parametrize(
    "changes",
    [
        {"user_access_token": SecretStr("")},
        {"user_access_token": SecretStr(" ")},
        {"user_access_token": SecretStr("u-line\r\ninjected")},
        {"user_access_token": SecretStr("a" * 8193)},
        {"user_access_token": "u-plain-string"},
        {"platform_tenant_id": ""},
        {"external_user_id": "open\x00id"},
        {"credential_revision": True},
        {"expires_at": datetime(2030, 1, 1)},
    ],
)
def test_invalid_employee_context_is_rejected_before_client_creation(changes):
    with pytest.raises(FeishuGatewayError) as error:
        FeishuUserGateway(binding(**changes))
    assert error.value.code == "invalid_context"


def test_expiry_is_rechecked_before_each_call_and_never_falls_back(remote):
    gateway = remote.gateway(binding(expires_at=datetime.now(UTC) + timedelta(seconds=0.1)))
    time.sleep(0.15)
    with pytest.raises(FeishuGatewayError) as error:
        asyncio.run(gateway.list_spaces())
    assert error.value.code == "credential_expired" and not remote.received


@pytest.mark.parametrize(
    "method,args,kwargs",
    [
        ("list_spaces", (), {"page_size": 51}),
        ("list_spaces", (), {"page_size": True}),
        ("list_nodes", ("space",), {"page_size": 0}),
        ("list_nodes", ("space",), {"parent_node_token": "bad\nparent"}),
        ("list_spaces", (), {"page_token": "x" * 8193}),
        ("list_document_blocks", ("doc",), {"revision_id": -1}),
        ("list_document_blocks", ("doc",), {"revision_id": True}),
        ("list_document_blocks", ("doc",), {"revision_id": 1, "page_size": 501}),
        ("get_document", ("x" * 513,), {}),
    ],
)
def test_invalid_parameters_never_reach_network(remote, method, args, kwargs):
    gateway = remote.gateway()
    with pytest.raises(FeishuGatewayError) as error:
        asyncio.run(getattr(gateway, method)(*args, **kwargs))
    assert error.value.code == "invalid_argument" and not remote.received


@pytest.mark.parametrize(
    "payload",
    [
        {"code": 0, "data": {"items": [], "has_more": 0}},
        {"code": 0, "data": {"items": []}},
        page(has_more=True),
        page(has_more=True, page_token="same"),
        page(has_more=True, page_token="bad\nnext"),
        page({"name": "missing ID"}),
        page({"space_id": "a"}, {"space_id": "b"}),
        {"code": False, "data": {"items": [], "has_more": False}},
    ],
)
def test_incomplete_or_coerced_pages_fail_closed(remote, payload):
    remote.replies.append({"payload": payload})
    with pytest.raises(FeishuGatewayError) as error:
        asyncio.run(remote.gateway().list_spaces(page_size=1, page_token="same"))
    assert error.value.code == "invalid_response"


def test_duplicate_ids_in_one_page_fail_closed(remote):
    remote.replies.append({"payload": page({"node_token": "same"}, {"node_token": "same"})})
    with pytest.raises(FeishuGatewayError) as error:
        asyncio.run(remote.gateway().list_nodes("space", page_size=2))
    assert error.value.code == "invalid_response"


@pytest.mark.parametrize(
    "status,code,expected,retryable",
    [
        (400, 99991677, "credential_expired", False),
        (400, 99991668, "credential_invalid", False),
        (403, 1770032, "permission_denied", False),
        (200, 99991672, "permission_denied", False),
        (400, 99991400, "rate_limited", True),
        (429, 987654, "rate_limited", True),
        (503, 987654, "upstream_unavailable", True),
        (400, 987654, "upstream_rejected", False),
    ],
)
def test_errors_are_classified_without_retries_or_upstream_body_leaks(
    remote, caplog, status, code, expected, retryable
):
    remote.replies.append(
        {"status": status, "payload": {"code": code, "msg": "u-synthetic-a private upstream message"}}
    )
    with pytest.raises(FeishuGatewayError) as error:
        asyncio.run(remote.gateway().list_spaces())
    assert error.value.code == expected and error.value.retryable is retryable
    assert len(remote.received) == 1
    assert "u-synthetic-a" not in repr(error.value) + caplog.text
    assert "private upstream" not in repr(error.value) + caplog.text
    assert error.value.__cause__ is None


def test_sdk_async_transport_does_not_follow_redirects(remote):
    remote.replies.append({"status": 302, "payload": page(), "headers": {"Location": "/credential-sink"}})
    with pytest.raises(FeishuGatewayError) as error:
        asyncio.run(remote.gateway().list_spaces())
    assert error.value.code == "redirect_rejected"
    assert len(remote.received) == 1


@pytest.mark.parametrize(
    "status,expected,retryable",
    [
        (401, "credential_invalid", False),
        (403, "permission_denied", False),
        (429, "rate_limited", True),
        (502, "upstream_unavailable", True),
        (503, "upstream_unavailable", True),
        (302, "redirect_rejected", False),
    ],
)
def test_non_json_http_failures_keep_transport_classification(remote, status, expected, retryable):
    remote.replies.append(
        {
            "status": status,
            "body": b"<html>private error u-synthetic-a</html>",
            "headers": {"Location": "/must-not-follow"} if status == 302 else {},
        }
    )
    with pytest.raises(FeishuGatewayError) as error:
        asyncio.run(remote.gateway().list_spaces())
    assert error.value.code == expected and error.value.retryable is retryable
    assert len(remote.received) == 1
    assert "private error" not in str(error.value) and "u-synthetic-a" not in repr(error.value)


def test_context_cannot_change_while_an_employee_read_is_in_flight(remote):
    gateway = remote.gateway()
    remote.replies.append({"payload": page({"space_id": "employee-a-space"}), "delay": 0.03})

    async def read():
        task = asyncio.create_task(gateway.list_spaces())
        await asyncio.sleep(0.01)
        with pytest.raises(AttributeError):
            gateway.context = binding(user_id="employee-b", user_access_token=SecretStr("u-synthetic-b"))
        return await task

    result = asyncio.run(read())
    assert result.binding.user_id == "employee-a"
    assert remote.received[0]["authorization"] == "Bearer u-synthetic-a"


def test_metadata_result_byte_budget_is_explicitly_after_sdk_buffering(remote, monkeypatch):
    from ontology.integrations.feishu import gateway as module

    monkeypatch.setattr(module, "MAX_RESPONSE_BYTES", 100)
    remote.replies.append({"payload": page({"space_id": "s1", "name": "x" * 200})})
    with pytest.raises(FeishuGatewayError) as error:
        asyncio.run(remote.gateway().list_spaces())
    assert error.value.code == "response_budget"


def test_real_http_timeout_is_safe_and_does_not_retry(remote):
    remote.replies.append({"payload": page(), "delay": 0.15})
    with pytest.raises(FeishuGatewayError) as error:
        asyncio.run(remote.gateway(timeout_seconds=0.02).list_spaces())
    assert error.value.code == "timed_out" and error.value.retryable
    assert len(remote.received) == 1


def test_malformed_success_json_is_not_permission_denial_or_empty_result(remote):
    remote.replies.append({"status": 200, "body": b"private malformed response u-synthetic-a"})
    with pytest.raises(FeishuGatewayError) as error:
        asyncio.run(remote.gateway().list_spaces())
    assert error.value.code == "invalid_response"
    assert "u-synthetic-a" not in repr(error.value)


@pytest.mark.parametrize(
    "body",
    [
        b'{"code":0,"data":{"items":[{"space_id":"s","metric":NaN}],"has_more":false}}',
        b'{"code":0,"data":{"items":[{"space_id":"s","metric":Infinity}],"has_more":false}}',
        b'{"code":0,"data":{"items":[{"space_id":"s","metric":-Infinity}],"has_more":false}}',
        b'{"code":0,"data":{"items":[{"space_id":"s","metric":1e400}],"has_more":false}}',
        b'{"code":99991677,"code":0,"data":{"items":[],"has_more":false}}',
        b'{"code":0,"data":{"items":[],"has_more":true,"has_more":false}}',
        b'{"code":0,"data":{"items":[{"space_id":"s","space_id":"other"}],"has_more":false}}',
    ],
)
def test_raw_json_rejects_nonfinite_numbers_and_duplicate_keys(remote, body):
    remote.replies.append({"status": 200, "body": body})
    with pytest.raises(FeishuGatewayError) as error:
        asyncio.run(remote.gateway().list_spaces())
    assert error.value.code == "invalid_response"
    assert not error.value.retryable and len(remote.received) == 1


def test_raw_json_valid_numbers_keep_existing_python_types(remote):
    remote.replies.append(
        {
            "body": b'{"code":0,"data":{"items":[{"space_id":"s","id":9007199254740993,'
            b'"ratio":1.25,"small":1e-20}],"has_more":false}}',
        }
    )
    result = asyncio.run(remote.gateway().list_spaces())
    item = result.items[0]
    assert type(item["id"]) is int and item["id"] == 9007199254740993
    assert type(item["ratio"]) is float and item["ratio"] == 1.25
    assert type(item["small"]) is float and item["small"] == 1e-20


@pytest.mark.parametrize(
    "document",
    [
        {"document_id": "different", "revision_id": 1, "title": "title"},
        {"document_id": "doc", "revision_id": True, "title": "title"},
        {"document_id": "doc", "revision_id": "1", "title": "title"},
    ],
)
def test_docx_metadata_does_not_accept_coerced_revision_or_other_document(remote, document):
    remote.replies.append({"payload": {"code": 0, "data": {"document": document}}})
    with pytest.raises(FeishuGatewayError) as error:
        asyncio.run(remote.gateway().get_document("doc"))
    assert error.value.code == "invalid_response"


def test_explicit_zero_revision_is_forwarded_without_assuming_it_exists(remote):
    remote.replies.append({"payload": page()})
    result = asyncio.run(remote.gateway().list_document_blocks("doc", revision_id=0))
    assert result.document_revision_id == 0
    assert parse_qs(urlsplit(remote.received[0]["path"]).query)["document_revision_id"] == ["0"]


@pytest.mark.parametrize(
    "kwargs",
    [
        {"service": "https://attacker.invalid"},
        {"timeout_seconds": float("nan")},
        {"timeout_seconds": 61},
        {"timeout_seconds": True},
    ],
)
def test_only_official_service_names_and_bounded_timeouts_are_accepted(kwargs):
    with pytest.raises(FeishuGatewayError) as error:
        FeishuUserGateway(binding(), **kwargs)
    assert error.value.code == "invalid_argument"
