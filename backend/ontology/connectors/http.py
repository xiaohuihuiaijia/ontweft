"""Bounded GET transport and explicit JSON resource adapter."""

from __future__ import annotations

import json
import uuid
from typing import Any
from urllib.parse import urljoin, urlsplit

import httpx
from pydantic import BaseModel, Field, SecretStr

from ontology.connectors.base import (
    ConnectionCheck,
    ConnectorBase,
    ConnectorCapabilities,
    ConnectorError,
    ResourceSchema,
    SnapshotPage,
    SourceRecord,
    validate_limit,
)
from ontology.connectors.policy import NetworkPolicy


class SafeHTTPClient:
    def __init__(
        self,
        base_url: str,
        *,
        network_policy: NetworkPolicy,
        token: SecretStr | None = None,
        max_bytes: int = 10 * 1024 * 1024,
        timeout: float = 20,
        client: httpx.Client | None = None,
    ):
        self.base_url = base_url.rstrip("/") + "/"
        self.policy, self._token = network_policy, token
        self.max_bytes, self.timeout = max_bytes, timeout
        self._client = client or httpx.Client(follow_redirects=False, trust_env=False)

    def get_bytes(self, path: str, params: dict | None = None) -> bytes:
        url = urljoin(self.base_url, path)
        if urlsplit(url).netloc != urlsplit(self.base_url).netloc:
            raise ConnectorError("network_denied", "Requests may not leave the configured origin")
        self.policy.check_url(url)
        headers = {"Authorization": "Bearer " + self._token.get_secret_value()} if self._token else {}
        try:
            with self._client.stream(
                "GET", url, params=params, headers=headers, timeout=self.timeout, follow_redirects=False
            ) as response:
                if response.status_code in {401, 403}:
                    raise ConnectorError("permission_denied", "Source credentials cannot read this resource")
                if response.status_code == 429:
                    raise ConnectorError("rate_limited", "Source request was rate limited", retryable=True)
                if response.status_code >= 500:
                    raise ConnectorError(
                        "source_unavailable", "Source is temporarily unavailable", retryable=True
                    )
                if not 200 <= response.status_code < 300:
                    raise ConnectorError(
                        "http_error", "Source returned an unsuccessful status; redirects are disabled"
                    )
                result = bytearray()
                for chunk in response.iter_bytes():
                    if len(result) + len(chunk) > self.max_bytes:
                        raise ConnectorError("resource_limit", "Source response exceeds its byte budget")
                    result.extend(chunk)
                return bytes(result)
        except ConnectorError:
            raise
        except httpx.HTTPError:
            raise ConnectorError("transport_error", "Source transport failed", retryable=True) from None

    def get_json(self, path: str, params: dict | None = None) -> Any:
        try:
            from decimal import Decimal

            return json.loads(self.get_bytes(path, params), parse_float=Decimal)
        except (ValueError, UnicodeError):
            raise ConnectorError("invalid_response", "Source response is not valid JSON") from None

    def close(self) -> None:
        self._client.close()


class HTTPResource(BaseModel):
    resource_id: str
    path: str
    key_fields: list[str] = Field(min_length=1)
    items_path: list[str] = Field(default_factory=lambda: ["items"])
    next_cursor_path: list[str] = Field(default_factory=lambda: ["next_cursor"])
    cursor_parameter: str = "cursor"
    limit_parameter: str = "limit"


def _lookup(value: Any, path: list[str]) -> Any:
    for key in path:
        if not isinstance(value, dict) or key not in value:
            return None
        value = value[key]
    return value


class HTTPConnector(ConnectorBase):
    capabilities = ConnectorCapabilities(
        notes=["Configured resources only; upstream pagination is not an atomic snapshot"]
    )

    def __init__(self, client: SafeHTTPClient, resources: list[HTTPResource]):
        self.client = client
        self.resources = {r.resource_id: r for r in resources}
        self._cursors: dict[str, tuple[str, str, Any, set[str]]] = {}

    def discover(self) -> list[ResourceSchema]:
        return [
            ResourceSchema(
                resource_id=r.resource_id,
                name=r.resource_id,
                kind="http_json",
                primary_key=r.key_fields,
                metadata={"discovery": "explicit_configuration"},
            )
            for r in self.resources.values()
        ]

    def test_connection(self) -> ConnectionCheck:
        if not self.resources:
            return ConnectionCheck(ok=False, code="no_resource", message="Select a resource first")
        try:
            self.sample(next(iter(self.resources)), 1)
            return ConnectionCheck(ok=True)
        except ConnectorError as exc:
            return ConnectionCheck(ok=False, code=exc.code, message=str(exc))

    def read_snapshot(self, resource: str, *, cursor: str | None = None, limit: int = 1000) -> SnapshotPage:
        validate_limit(limit)
        config = self.resources.get(resource)
        if config is None:
            raise ConnectorError("unknown_resource", "HTTP resource is not configured")
        snapshot, upstream, seen = str(uuid.uuid4()), None, set()
        if cursor:
            state = self._cursors.get(cursor)
            if state is None or state[0] != resource:
                raise ConnectorError("invalid_cursor", "HTTP cursor is invalid or expired")
            _, snapshot, upstream, seen = state
        params = {config.limit_parameter: limit}
        if upstream is not None:
            params[config.cursor_parameter] = upstream
        value = self.client.get_json(config.path, params)
        items = _lookup(value, config.items_path)
        if not isinstance(items, list) or len(items) > limit:
            raise ConnectorError(
                "invalid_response", "HTTP item list is missing or exceeds the requested limit"
            )
        records = []
        for item in items:
            if not isinstance(item, dict) or any(k not in item or item[k] is None for k in config.key_fields):
                raise ConnectorError(
                    "invalid_record_key", "Source record is missing its configured stable key"
                )
            records.append(SourceRecord(key={k: item[k] for k in config.key_fields}, values=item))
        token = _lookup(value, config.next_cursor_path)
        parent = _lookup(value, config.next_cursor_path[:-1]) if config.next_cursor_path else None
        if (
            not isinstance(parent, dict)
            or not config.next_cursor_path
            or config.next_cursor_path[-1] not in parent
        ):
            raise ConnectorError(
                "incomplete_response", "Source response omits its configured pagination completion field"
            )
        next_cursor = None
        if token not in (None, ""):
            if not isinstance(token, (str, int)):
                raise ConnectorError("invalid_response", "Source continuation token is not a scalar")
            signature = str(token)
            if signature in seen:
                raise ConnectorError("pagination_loop", "Source repeated a pagination token")
            seen.add(signature)
            next_cursor = str(uuid.uuid4())
            self._cursors[next_cursor] = (resource, snapshot, token, seen)
        if cursor:
            self._cursors.pop(cursor, None)
        return SnapshotPage(
            resource_id=resource,
            snapshot_id=snapshot,
            complete=next_cursor is None,
            next_cursor=next_cursor,
            records=records,
            consistency="best_effort",
            warnings=["Source API provides no atomic snapshot; incomplete scans cannot confirm deletions"],
        )

    def sample(self, resource: str, limit: int = 20) -> SnapshotPage:
        page = self.read_snapshot(resource, limit=limit)
        if page.next_cursor:
            self._cursors.pop(page.next_cursor, None)
        return page.model_copy(update={"complete": False, "next_cursor": None})

    def close(self) -> None:
        self._cursors.clear()
        self.client.close()
