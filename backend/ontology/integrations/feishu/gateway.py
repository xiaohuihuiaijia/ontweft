"""Single-page employee reads using the pinned official lark-oapi SDK.

The context is caller-supplied, NOT proof of a persisted account binding or of
resource/row/field authorization. A future service must verify that binding,
revocation and cursor scope before every call and before publishing results.
No application credentials, token refresh, global cache configuration, automatic
pagination, import, attachment download or source-deletion inference lives here.

Generated business methods unconditionally deserialize JSON, losing HTTP error
classification on empty/HTML responses. We reuse their official request models,
SDK token verification and SDK async transport, then inspect HTTP before JSON.
SDK async transport does not follow redirects by default, but uses environment
proxy settings and buffers metadata responses. The byte check below is a result
budget, not a streaming download limit. Binary downloads must use the existing
controlled streaming transport. No production SDK transport monkeypatch is used.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal

import httpx
from pydantic import SecretStr

OFFICIAL_DOMAINS = {"feishu": "https://open.feishu.cn", "lark": "https://open.larksuite.com"}
MAX_RESPONSE_BYTES = 8 * 1024 * 1024

_MESSAGES = {
    "invalid_context": "Employee binding context is missing or invalid",
    "invalid_argument": "The requested resource or page parameters are invalid",
    "credential_expired": "The employee credential has expired",
    "credential_invalid": "The employee credential was rejected",
    "permission_denied": "The current employee cannot perform this read",
    "rate_limited": "The upstream service rate limited this read",
    "upstream_unavailable": "The upstream service is temporarily unavailable",
    "upstream_rejected": "The upstream service rejected this read with an unclassified error",
    "transport_error": "The upstream transport failed",
    "timed_out": "The upstream read timed out",
    "invalid_response": "The upstream response is incomplete or invalid",
    "response_budget": "The upstream response exceeds the result budget",
    "redirect_rejected": "The upstream service returned a redirect",
}


class FeishuGatewayError(Exception):
    """Safe classification only: never retain the upstream body, message or token."""

    def __init__(self, code: str, *, retryable: bool = False):
        self.code, self.retryable = code, retryable
        super().__init__(_MESSAGES[code])


def _text(value: Any, *, limit: int = 512, code: str = "invalid_argument") -> str:
    if (
        not isinstance(value, str)
        or not value.strip()
        or len(value) > limit
        or any(ord(char) < 32 or ord(char) == 127 for char in value)
    ):
        raise FeishuGatewayError(code)
    return value


def _integer(value: Any, *, minimum: int, maximum: int) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        raise FeishuGatewayError("invalid_argument")
    return value


def _reject_json_constant(_value: str) -> Any:
    raise ValueError("Non-finite JSON constant")


def _finite_json_float(value: str) -> float:
    result = float(value)
    if not math.isfinite(result):
        raise ValueError("Non-finite JSON number")
    return result


def _unique_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON object key")
        result[key] = value
    return result


@dataclass(frozen=True)
class BindingReference:
    platform_tenant_id: str
    user_id: str
    connection_id: str
    external_tenant_key: str
    external_user_id: str
    credential_revision: int


@dataclass(frozen=True)
class EmployeeBindingContext(BindingReference):
    """A caller assertion, not a database binding or a source permission grant."""

    user_access_token: SecretStr = field(repr=False)
    expires_at: datetime

    def validate(self) -> None:
        for value in (
            self.platform_tenant_id,
            self.user_id,
            self.connection_id,
            self.external_tenant_key,
            self.external_user_id,
        ):
            _text(value, code="invalid_context")
        if type(self.credential_revision) is not int or self.credential_revision < 1:
            raise FeishuGatewayError("invalid_context")
        if not isinstance(self.user_access_token, SecretStr):
            raise FeishuGatewayError("invalid_context")
        token = self.user_access_token.get_secret_value()
        if not token or len(token) > 8192 or any(not 33 <= ord(char) <= 126 for char in token):
            raise FeishuGatewayError("invalid_context")
        if not isinstance(self.expires_at, datetime) or self.expires_at.utcoffset() is None:
            raise FeishuGatewayError("invalid_context")
        if self.expires_at <= datetime.now(UTC):
            raise FeishuGatewayError("credential_expired")

    def reference(self) -> BindingReference:
        return BindingReference(
            self.platform_tenant_id,
            self.user_id,
            self.connection_id,
            self.external_tenant_key,
            self.external_user_id,
            self.credential_revision,
        )


@dataclass(frozen=True)
class SdkPage:
    binding: BindingReference
    items: tuple[dict[str, Any], ...]
    has_more: bool
    next_page_token: str | None
    document_revision_id: int | None = None


@dataclass(frozen=True)
class DocumentMetadata:
    binding: BindingReference
    document_id: str
    revision_id: int
    title: str


@dataclass(frozen=True)
class WikiNode:
    binding: BindingReference
    space_id: str
    node_token: str
    resource_type: str
    resource_token: str
    node_type: str
    origin_node_token: str | None
    origin_space_id: str | None
    has_child: bool
    title: str


@dataclass(frozen=True)
class ResourcePermission:
    binding: BindingReference
    resource_type: str
    resource_token: str
    action: str
    allowed: bool


class FeishuUserGateway:
    def __init__(
        self,
        context: EmployeeBindingContext,
        *,
        service: Literal["feishu", "lark"] = "feishu",
        timeout_seconds: float = 20,
    ):
        if not isinstance(context, EmployeeBindingContext):
            raise FeishuGatewayError("invalid_context")
        context.validate()
        if (
            service not in OFFICIAL_DOMAINS
            or type(timeout_seconds) not in {int, float}
            or not math.isfinite(timeout_seconds)
            or not 0 < timeout_seconds <= 60
        ):
            raise FeishuGatewayError("invalid_argument")
        # The SDK eagerly imports its full generated API catalogue. Keep that
        # cost out of core application startup; load it only for a valid read.
        import lark_oapi as lark

        self._context = context
        # No app_id/app_secret, assertion provider or custom cache. Even a
        # future missing-user-token regression cannot fetch a tenant token.
        self._client = (
            lark.Client.builder()
            .domain(OFFICIAL_DOMAINS[service])
            .enable_set_token(True)
            .timeout(timeout_seconds)
            .build()
        )

    @property
    def context(self) -> EmployeeBindingContext:
        return self._context

    async def _call(self, request: Any) -> dict:
        self.context.validate()
        import lark_oapi as lark
        from lark_oapi.core.enum import AccessTokenType
        from lark_oapi.core.http.transport import Transport
        from lark_oapi.core.token import verify

        # SDK verify mutates this set; every public operation builds a new request.
        request.token_types = {AccessTokenType.USER}
        option = (
            lark.RequestOption.builder()
            .user_access_token(self.context.user_access_token.get_secret_value())
            .build()
        )
        try:
            verify(self._client._config, request, option)
            raw = await Transport.aexecute(self._client._config, request, option)
        except httpx.TimeoutException:
            raise FeishuGatewayError("timed_out", retryable=True) from None
        except httpx.HTTPError:
            raise FeishuGatewayError("transport_error", retryable=True) from None
        except (ValueError, TypeError, UnicodeError, AttributeError):
            raise FeishuGatewayError("invalid_response") from None
        except Exception:  # noqa: BLE001 - untrusted SDK exceptions must not expose credentials.
            raise FeishuGatewayError("upstream_rejected") from None
        if raw is None or type(raw.status_code) is not int or not isinstance(raw.content, bytes):
            raise FeishuGatewayError("invalid_response")
        status = raw.status_code
        if 300 <= status < 400:
            raise FeishuGatewayError("redirect_rejected")
        if status == 401:
            raise FeishuGatewayError("credential_invalid")
        if status == 403:
            raise FeishuGatewayError("permission_denied")
        if status == 429:
            raise FeishuGatewayError("rate_limited", retryable=True)
        if status >= 500:
            raise FeishuGatewayError("upstream_unavailable", retryable=True)
        if len(raw.content) > MAX_RESPONSE_BYTES:
            raise FeishuGatewayError("response_budget")
        # Inspect the raw JSON, not SDK-coerced booleans/integers. Preserve raw
        # block structure and new fields rather than flattening their text.
        try:
            payload = json.loads(
                raw.content,
                parse_constant=_reject_json_constant,
                parse_float=_finite_json_float,
                object_pairs_hook=_unique_json_object,
            )
        except (ValueError, UnicodeError):
            raise FeishuGatewayError("invalid_response") from None
        if not isinstance(payload, dict) or type(payload.get("code")) is not int:
            raise FeishuGatewayError("invalid_response")
        code = payload["code"]
        # Verified codes: official generic-error-code and Docx document/list.
        # 99991668 is ambiguous invalid/unsupported, not proven token expiry.
        if code == 99991677:
            raise FeishuGatewayError("credential_expired")
        if code in {99991661, 99991663, 99991668, 99991671}:
            raise FeishuGatewayError("credential_invalid")
        if code in {99991672, 99991676, 99991679, 1770032, 131006}:
            raise FeishuGatewayError("permission_denied")
        if code == 99991400:
            raise FeishuGatewayError("rate_limited", retryable=True)
        if status != 200 or code != 0:
            raise FeishuGatewayError("upstream_rejected")
        if not isinstance(payload.get("data"), dict):
            raise FeishuGatewayError("invalid_response")
        return payload["data"]

    def _page(self, data, *, key, page_size, previous_token, revision_id=None) -> SdkPage:
        items, has_more = data.get("items"), data.get("has_more")
        if not isinstance(items, list) or len(items) > page_size or type(has_more) is not bool:
            raise FeishuGatewayError("invalid_response")
        identities = set()
        for item in items:
            if not isinstance(item, dict):
                raise FeishuGatewayError("invalid_response")
            identity = _text(item.get(key), code="invalid_response")
            if identity in identities:
                raise FeishuGatewayError("invalid_response")
            identities.add(identity)
        next_token = None
        if has_more:
            next_token = _text(data.get("page_token"), limit=8192, code="invalid_response")
            if next_token == previous_token:
                raise FeishuGatewayError("invalid_response")
        return SdkPage(self.context.reference(), tuple(items), has_more, next_token, revision_id)

    async def list_spaces(self, *, page_size: int = 50, page_token: str | None = None) -> SdkPage:
        from lark_oapi.api.wiki.v2 import ListSpaceRequest

        _integer(page_size, minimum=1, maximum=50)
        builder = ListSpaceRequest.builder().page_size(page_size)
        if page_token is not None:
            builder.page_token(_text(page_token, limit=8192))
        data = await self._call(builder.build())
        return self._page(data, key="space_id", page_size=page_size, previous_token=page_token)

    async def list_nodes(
        self,
        space_id: str,
        *,
        parent_node_token: str | None = None,
        page_size: int = 50,
        page_token: str | None = None,
    ) -> SdkPage:
        from lark_oapi.api.wiki.v2 import ListSpaceNodeRequest

        _integer(page_size, minimum=1, maximum=50)
        builder = ListSpaceNodeRequest.builder().space_id(_text(space_id)).page_size(page_size)
        if parent_node_token is not None:
            builder.parent_node_token(_text(parent_node_token))
        if page_token is not None:
            builder.page_token(_text(page_token, limit=8192))
        data = await self._call(builder.build())
        return self._page(data, key="node_token", page_size=page_size, previous_token=page_token)

    async def get_document(self, document_id: str) -> DocumentMetadata:
        from lark_oapi.api.docx.v1 import GetDocumentRequest

        request = GetDocumentRequest.builder().document_id(_text(document_id)).build()
        data = await self._call(request)
        doc = data.get("document")
        if (
            not isinstance(doc, dict)
            or doc.get("document_id") != document_id
            or type(doc.get("revision_id")) is not int
            or doc["revision_id"] < 0
            or not isinstance(doc.get("title"), str)
        ):
            raise FeishuGatewayError("invalid_response")
        return DocumentMetadata(self.context.reference(), document_id, doc["revision_id"], doc["title"])

    async def get_wiki_node(self, node_token: str) -> WikiNode:
        from lark_oapi.api.wiki.v2 import GetNodeSpaceRequest

        # Always resolve a Wiki location. Never treat its node token as a Docx/file token.
        request = GetNodeSpaceRequest.builder().token(_text(node_token)).obj_type("wiki").build()
        data = await self._call(request)
        node = data.get("node")
        if (
            not isinstance(node, dict)
            or node.get("node_token") != node_token
            or node.get("node_type") not in {"origin", "shortcut"}
            or type(node.get("has_child")) is not bool
            or not isinstance(node.get("title"), str)
        ):
            raise FeishuGatewayError("invalid_response")
        origin_token, origin_space = None, None
        if node["node_type"] == "shortcut":
            origin_token = _text(node.get("origin_node_token"), code="invalid_response")
            origin_space = _text(node.get("origin_space_id"), code="invalid_response")
        return WikiNode(
            self.context.reference(),
            _text(node.get("space_id"), code="invalid_response"),
            node_token,
            _text(node.get("obj_type"), code="invalid_response"),
            _text(node.get("obj_token"), code="invalid_response"),
            node["node_type"],
            origin_token,
            origin_space,
            node["has_child"],
            node["title"],
        )

    async def check_permission(
        self, resource_type: str, resource_token: str, *, action: str
    ) -> ResourcePermission:
        from lark_oapi.api.drive.v1 import AuthPermissionMemberRequest

        # These are observation operations, not permission mutations. Unknown,
        # failed or missing auth_result is never interpreted as an explicit denial.
        if resource_type not in {
            "doc",
            "docx",
            "sheet",
            "file",
            "wiki",
            "bitable",
            "mindnote",
            "minutes",
            "slides",
        } or action not in {"view", "copy", "export"}:
            raise FeishuGatewayError("invalid_argument")
        request = (
            AuthPermissionMemberRequest.builder()
            .token(_text(resource_token))
            .type(resource_type)
            .action(action)
            .build()
        )
        data = await self._call(request)
        if type(data.get("auth_result")) is not bool:
            raise FeishuGatewayError("invalid_response")
        return ResourcePermission(
            self.context.reference(), resource_type, resource_token, action, data["auth_result"]
        )

    async def list_document_blocks(
        self,
        document_id: str,
        *,
        revision_id: int,
        page_size: int = 500,
        page_token: str | None = None,
    ) -> SdkPage:
        from lark_oapi.api.docx.v1 import ListDocumentBlockRequest

        # -1 means latest, which cannot pin a multi-page read. Accept explicit
        # nonnegative revisions; the server decides whether that revision exists.
        _integer(revision_id, minimum=0, maximum=2**63 - 1)
        _integer(page_size, minimum=1, maximum=500)
        builder = (
            ListDocumentBlockRequest.builder()
            .document_id(_text(document_id))
            .document_revision_id(revision_id)
            .page_size(page_size)
            .user_id_type("open_id")
        )
        if page_token is not None:
            builder.page_token(_text(page_token, limit=8192))
        data = await self._call(builder.build())
        return self._page(
            data, key="block_id", page_size=page_size, previous_token=page_token, revision_id=revision_id
        )
