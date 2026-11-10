"""Read selected Feishu resources with supplied credentials; no app-token fallback.

Remote row/field authorization semantics require tenant-specific acceptance.
These adapters never turn a successful app read into employee authorization.
"""

from __future__ import annotations

import hashlib
import uuid
from typing import Literal
from urllib.parse import quote

from pydantic import BaseModel

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
from ontology.connectors.http import SafeHTTPClient


class FeishuResource(BaseModel):
    resource_id: str
    kind: Literal["wiki_nodes", "docx", "sheets", "bitable", "attachment"]
    resource_token: str
    table_id: str | None = None
    range: str | None = None
    parent_node_token: str | None = None


class FeishuConnector(ConnectorBase):
    capabilities = ConnectorCapabilities(
        notes=[
            "Selected resources only; Wiki lists direct children of the selected parent",
            "No atomic remote snapshot; failed or incomplete pages cannot confirm deletion",
            "Employee record/field permission semantics are not certified by successful application reads",
        ]
    )

    def __init__(
        self, client: SafeHTTPClient, resources: list[FeishuResource], *, credential_kind: str = "user"
    ):
        if credential_kind not in {"user", "application"}:
            raise ConnectorError("invalid_identity", "Specify user or application credential identity")
        self.client, self.credential_kind = client, credential_kind
        self.resources = {r.resource_id: r for r in resources}
        self._cursors: dict[str, tuple[str, str, str, set[str]]] = {}

    def _get(self, path: str, params: dict | None = None) -> dict:
        payload = self.client.get_json(path, params)
        if not isinstance(payload, dict):
            raise ConnectorError("invalid_response", "Feishu returned an invalid response")
        code = payload.get("code")
        if code == 99991400:
            raise ConnectorError("rate_limited", "Feishu request was rate limited", retryable=True)
        if code != 0:
            # Do not echo upstream msg, which can contain supplied tokens/parameters.
            raise ConnectorError("source_rejected", "Feishu rejected the current identity or request")
        data = payload.get("data")
        if not isinstance(data, dict):
            raise ConnectorError("invalid_response", "Feishu response lacks a data object")
        return data

    def wiki_node(self, token: str) -> dict:
        return self._get("wiki/v2/spaces/get_node", {"token": token})

    def sheet_metadata(self, token: str) -> dict:
        return self._get(f"sheets/v3/spreadsheets/{quote(token, safe='')}/sheets/query")

    def bitable_fields(self, app_token: str, table_id: str, *, page_token: str | None = None) -> dict:
        params = {"page_size": 100}
        if page_token:
            params["page_token"] = page_token
        return self._get(
            f"bitable/v1/apps/{quote(app_token, safe='')}/tables/{quote(table_id, safe='')}/fields", params
        )

    def discover(self) -> list[ResourceSchema]:
        return [
            ResourceSchema(
                resource_id=r.resource_id,
                name=r.resource_id,
                kind="feishu_" + r.kind,
                metadata={"discovery": "explicit_selection", "credential_kind": self.credential_kind},
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
            raise ConnectorError("unknown_resource", "Feishu resource is outside the selected scope")
        snapshot, upstream, seen = str(uuid.uuid4()), None, set()
        if cursor:
            state = self._cursors.get(cursor)
            if state is None or state[0] != resource:
                raise ConnectorError("invalid_cursor", "Feishu cursor is expired or mismatched")
            _, snapshot, upstream, seen = state
        token = quote(config.resource_token, safe="")
        params = {"page_size": min(limit, 100)}
        if upstream:
            params["page_token"] = upstream
        if config.kind == "wiki_nodes":
            path, key = f"wiki/v2/spaces/{token}/nodes", "node_token"
            if config.parent_node_token:
                params["parent_node_token"] = config.parent_node_token
        elif config.kind == "docx":
            path, key = f"docx/v1/documents/{token}/blocks", "block_id"
        elif config.kind == "bitable":
            if not config.table_id:
                raise ConnectorError("invalid_config", "Bitable requires a table ID")
            path, key = (
                f"bitable/v1/apps/{token}/tables/{quote(config.table_id, safe='')}/records",
                "record_id",
            )
        elif config.kind == "sheets":
            if cursor or not config.range:
                raise ConnectorError(
                    "invalid_config", "Sheets requires an explicit bounded range and no cursor"
                )
            data = self._get(f"sheets/v2/spreadsheets/{token}/values/{quote(config.range, safe='')}")
            return SnapshotPage(
                resource_id=resource,
                snapshot_id=snapshot,
                complete=True,
                records=[SourceRecord(key={"range": config.range}, values=data)],
                warnings=[
                    "Only the explicitly selected range was collected; formulas and merged cells need separate metadata"
                ],
            )
        else:
            if cursor:
                raise ConnectorError("invalid_cursor", "Attachment download has one page")
            blob = self.client.get_bytes(f"drive/v1/medias/{token}/download")
            digest = hashlib.sha256(blob).hexdigest()
            return SnapshotPage(
                resource_id=resource,
                snapshot_id=digest,
                complete=True,
                records=[
                    SourceRecord(
                        key={"file_token": config.resource_token},
                        values={"content": blob, "content_hash": digest},
                    )
                ],
            )
        data = self._get(path, params)
        items = data.get("items")
        if (
            not isinstance(items, list)
            or len(items) > params["page_size"]
            or not isinstance(data.get("has_more"), bool)
        ):
            raise ConnectorError("invalid_response", "Feishu returned an invalid or oversized resource page")
        records = []
        for item in items:
            if not isinstance(item, dict) or not item.get(key):
                raise ConnectorError("invalid_record_key", "Feishu resource lacks its stable ID")
            records.append(SourceRecord(key={key: item[key]}, values=item))
        next_cursor = None
        if data.get("has_more"):
            next_token = data.get("page_token")
            if not isinstance(next_token, str) or not next_token or next_token in seen:
                raise ConnectorError("pagination_loop", "Feishu pagination is incomplete or repeated")
            seen.add(next_token)
            next_cursor = str(uuid.uuid4())
            self._cursors[next_cursor] = (resource, snapshot, next_token, seen)
        if cursor:
            self._cursors.pop(cursor, None)
        return SnapshotPage(
            resource_id=resource,
            snapshot_id=snapshot,
            next_cursor=next_cursor,
            complete=next_cursor is None,
            records=records,
            warnings=[
                "No atomic source snapshot; preserve the collection window and current source authorization"
            ],
        )

    def sample(self, resource: str, limit: int = 20) -> SnapshotPage:
        page = self.read_snapshot(resource, limit=limit)
        if page.next_cursor:
            self._cursors.pop(page.next_cursor, None)
        return page.model_copy(update={"complete": False, "next_cursor": None})

    def close(self) -> None:
        self._cursors.clear()
        self.client.close()
