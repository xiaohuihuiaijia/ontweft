"""Source-side contracts. Persistence, tenant authorization and CDC live elsewhere."""

from __future__ import annotations

import base64
import math
from datetime import UTC, date, datetime, time
from decimal import Decimal
from typing import Any, Literal, Protocol, runtime_checkable
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class ConnectorError(Exception):
    """Safe, stable error; never put driver messages or credentials in public errors."""

    def __init__(self, code: str, message: str, *, retryable: bool = False):
        super().__init__(message)
        self.code, self.retryable = code, retryable


class ConnectorCapabilities(BaseModel):
    discover: bool = True
    sample: bool = True
    snapshot: bool = True
    incremental: bool = False
    deletion_reconciliation: bool = False
    source_permissions: bool = False
    consistent_snapshot: bool = False
    resumable_after_restart: bool = False
    notes: list[str] = Field(default_factory=list)


class FieldSchema(BaseModel):
    name: str
    data_type: str
    nullable: bool = True
    ordinal: int = 0
    metadata: dict[str, Any] = Field(default_factory=dict)


class ForeignKey(BaseModel):
    fields: list[str]
    target_resource: str
    target_fields: list[str]


class ResourceSchema(BaseModel):
    resource_id: str
    name: str
    namespace: str | None = None
    kind: str = "table"
    fields: list[FieldSchema] = Field(default_factory=list)
    primary_key: list[str] = Field(default_factory=list)
    foreign_keys: list[ForeignKey] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


class SourceRecord(BaseModel):
    model_config = ConfigDict(ser_json_bytes="base64")
    key: dict[str, Any]
    identity_key: dict[str, Any] | None = None
    values: dict[str, Any]


def canonical_identity_key(key: dict[str, Any]) -> dict[str, dict[str, str]]:
    """Lossless scalar identities; database collation semantics need separate validation."""
    result = {}
    for field, value in key.items():
        if isinstance(value, Decimal):
            if not value.is_finite():
                raise ConnectorError(
                    "unsupported_key_semantics", "Non-finite numeric keys require an explicit stable identity"
                )
            if value.is_zero():
                canonical = "0"
            else:
                sign, digits, exponent = value.as_tuple()
                digits = list(digits)
                while digits[-1] == 0:
                    digits.pop()
                    exponent += 1
                canonical = (
                    ("-" if sign else "") + "".join(map(str, digits)) + (f"e{exponent}" if exponent else "")
                )
            kind = "decimal"
        elif isinstance(value, bool):
            kind, canonical = "boolean", "true" if value else "false"
        elif isinstance(value, int):
            kind, canonical = "integer", str(value)
        elif isinstance(value, str):
            kind, canonical = "string", value
        elif isinstance(value, (bytes, bytearray, memoryview)):
            kind, canonical = "bytes", base64.b64encode(bytes(value)).decode("ascii")
        elif isinstance(value, UUID):
            kind, canonical = "uuid", str(value)
        elif isinstance(value, datetime):
            if value.tzinfo is not None:
                kind, canonical = "instant", value.astimezone(UTC).isoformat()
            else:
                kind, canonical = "local_datetime", value.isoformat()
        elif isinstance(value, date):
            kind, canonical = "date", value.isoformat()
        elif isinstance(value, time) and value.tzinfo is None:
            kind, canonical = "local_time", value.isoformat()
        elif isinstance(value, float) and math.isfinite(value):
            kind, canonical = "float64", (0.0 if value == 0 else value).hex()
        else:
            raise ConnectorError(
                "unsupported_key_semantics", "This key type requires an explicit stable identity"
            )
        result[field] = {"type": kind, "value": canonical}
    return result


class SnapshotPage(BaseModel):
    model_config = ConfigDict(ser_json_bytes="base64")
    records: list[SourceRecord]
    resource_id: str
    snapshot_id: str
    next_cursor: str | None = None
    complete: bool
    consistency: Literal["transaction", "collection", "best_effort"] = "best_effort"
    schema_fingerprint: str | None = None
    warnings: list[str] = Field(default_factory=list)


class ConnectionCheck(BaseModel):
    ok: bool
    code: str = "ok"
    message: str = "Connection available"


class SourcePermission(BaseModel):
    decision: Literal["allow", "deny", "unknown"] = "unknown"
    reason: str = "Source authorization must be checked by the caller"


@runtime_checkable
class Connector(Protocol):
    capabilities: ConnectorCapabilities

    def test_connection(self) -> ConnectionCheck: ...
    def discover(self) -> list[ResourceSchema]: ...
    def sample(self, resource: str, limit: int = 20) -> SnapshotPage: ...
    def read_snapshot(
        self, resource: str, *, cursor: str | None = None, limit: int = 1000
    ) -> SnapshotPage: ...
    def close(self) -> None: ...


class ConnectorBase:
    capabilities = ConnectorCapabilities()

    def read_changes(self, *args: Any, **kwargs: Any) -> Any:
        raise ConnectorError("unsupported_capability", "Use the separately configured CDC engine")

    def reconcile_deletions(self, *args: Any, **kwargs: Any) -> Any:
        raise ConnectorError("unsupported_capability", "Deletion requires a complete authorized inventory")

    def source_permission(self, *args: Any, **kwargs: Any) -> SourcePermission:
        return SourcePermission()

    def health(self) -> ConnectionCheck:
        return self.test_connection()


def validate_limit(limit: int, maximum: int = 10_000) -> None:
    if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= maximum:
        raise ConnectorError("invalid_limit", f"Limit must be between 1 and {maximum}")
