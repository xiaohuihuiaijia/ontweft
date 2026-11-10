"""MongoDB bounded reads. Change-stream consistency belongs to the CDC engine."""

from __future__ import annotations

import base64
import uuid
from datetime import UTC, datetime
from itertools import islice
from typing import Any

from pydantic import BaseModel, Field, SecretStr

from ontology.connectors.base import (
    ConnectionCheck,
    ConnectorBase,
    ConnectorCapabilities,
    ConnectorError,
    FieldSchema,
    ResourceSchema,
    SnapshotPage,
    SourceRecord,
    canonical_identity_key,
    validate_limit,
)
from ontology.connectors.policy import NetworkPolicy


class MongoConfig(BaseModel):
    host: str
    port: int = 27017
    database: str
    username: str | None = None
    password: SecretStr | None = Field(default=None, repr=False)
    auth_source: str = "admin"
    collections: list[str] = Field(default_factory=list)
    timeout_seconds: int = Field(default=15, ge=1, le=300)
    tls: bool = True


class MongoConnector(ConnectorBase):
    capabilities = ConnectorCapabilities(
        notes=[
            "Bounded collection reads are not a transaction-consistent snapshot",
            "BSON identity/types use canonical Extended JSON; missing fields remain absent",
            "Change streams and resume tokens require the separate CDC path",
            "Server cursor enumeration preserves mixed BSON _id types; lost cursors require restarting the scan",
            "Identities preserve embedded document field order and unify integer widths; floating/decimal IDs require explicit canonical source keys",
        ]
    )

    def __init__(self, config: MongoConfig, *, network_policy: NetworkPolicy):
        self.config, self.policy = config, network_policy
        self._client: Any = None
        self._resources: set[str] | None = None
        self._cursors: dict[str, dict[str, Any]] = {}

    def _db(self):
        try:
            if self._client is None:
                self.policy.check_host(self.config.host, self.config.port)
                from pymongo import MongoClient

                self._client = MongoClient(
                    self.config.host,
                    self.config.port,
                    username=self.config.username,
                    password=self.config.password.get_secret_value() if self.config.password else None,
                    authSource=self.config.auth_source,
                    tls=self.config.tls,
                    serverSelectionTimeoutMS=self.config.timeout_seconds * 1000,
                    socketTimeoutMS=self.config.timeout_seconds * 1000,
                    directConnection=True,
                )
            return self._client[self.config.database]
        except ConnectorError:
            raise
        except ImportError:
            raise ConnectorError("driver_unavailable", "MongoDB driver is not installed") from None
        except Exception:  # noqa: BLE001 -- public adapter boundary redacts untrusted driver errors.
            raise ConnectorError("connection_failed", "MongoDB connection failed", retryable=True) from None

    def test_connection(self) -> ConnectionCheck:
        try:
            self._db().command("ping")
            return ConnectionCheck(ok=True)
        except Exception as exc:  # noqa: BLE001 -- public status must not expose driver credentials.
            return ConnectionCheck(
                ok=False,
                code=exc.code if isinstance(exc, ConnectorError) else "connection_failed",
                message="MongoDB connection check failed",
            )

    def discover(self) -> list[ResourceSchema]:
        try:
            names = self._db().list_collection_names()
            if self.config.collections:
                if not set(self.config.collections).issubset(names):
                    raise ConnectorError(
                        "incomplete_discovery", "A selected collection is missing or inaccessible"
                    )
                names = self.config.collections
            self._resources = set(names)
            return [
                ResourceSchema(
                    resource_id=name,
                    name=name,
                    namespace=self.config.database,
                    kind="collection",
                    primary_key=["_id"],
                    fields=[FieldSchema(name="_id", data_type="bson", nullable=False)],
                    metadata={"schema": "dynamic; inspect bounded samples"},
                )
                for name in sorted(names)
            ]
        except ConnectorError:
            raise
        except Exception:  # noqa: BLE001 -- public adapter boundary redacts untrusted driver errors.
            raise ConnectorError(
                "source_read_failed", "MongoDB collections could not be discovered"
            ) from None

    def read_snapshot(self, resource: str, *, cursor: str | None = None, limit: int = 1000) -> SnapshotPage:
        validate_limit(limit)
        if self._resources is None:
            self.discover()
        if resource not in self._resources:
            raise ConnectorError("unknown_resource", "Collection is outside the selected scope")
        state = None
        if cursor:
            state = self._cursors.get(cursor)
            if state is None or state["resource"] != resource:
                raise ConnectorError("invalid_cursor", "Collection cursor is expired or mismatched")
        try:
            import json

            from bson import json_util

            if state is None:
                state = {
                    "resource": resource,
                    "snapshot": str(uuid.uuid4()),
                    "buffer": [],
                    "cursor": self._db()[resource]
                    .find({})
                    .sort("_id", 1)
                    .batch_size(limit + 1)
                    .max_time_ms(self.config.timeout_seconds * 1000),
                }
            # $gt on a previous _id applies BSON type bracketing and skips other
            # ID types. Continue the actual server cursor, not a guessed boundary.
            docs = state["buffer"] + list(islice(state["cursor"], limit + 1 - len(state["buffer"])))
            state["buffer"] = docs[limit:]
            records = []
            for doc in docs[:limit]:
                values = json.loads(json_util.dumps(doc, json_options=json_util.CANONICAL_JSON_OPTIONS))
                records.append(
                    SourceRecord(
                        key={"_id": values["_id"]},
                        identity_key={"_id": self._identity(doc["_id"])},
                        values=values,
                    )
                )
            next_cursor = None
            if len(docs) > limit:
                next_cursor = str(uuid.uuid4())
                self._cursors[next_cursor] = state
            else:
                state["cursor"].close()
            if cursor:
                self._cursors.pop(cursor, None)
            return SnapshotPage(
                resource_id=resource,
                snapshot_id=state["snapshot"],
                next_cursor=next_cursor,
                complete=next_cursor is None,
                records=records,
                consistency="best_effort",
                warnings=[
                    "Concurrent source changes can affect pagination; use CDC for a consistent baseline"
                ],
            )
        except ConnectorError:
            if cursor:
                self._cursors.pop(cursor, None)
            if state:
                self._close_cursor(state)
            raise
        except Exception:  # noqa: BLE001 -- public adapter boundary redacts untrusted driver errors.
            if cursor:
                self._cursors.pop(cursor, None)
            if state:
                self._close_cursor(state)
            raise ConnectorError(
                "snapshot_lost" if cursor else "source_read_failed",
                "MongoDB scan failed; restart enumeration",
                retryable=True,
            ) from None

    @staticmethod
    def _identity(value: Any) -> dict:
        from bson import Binary, Decimal128, ObjectId

        if isinstance(value, ObjectId):
            return {"type": "bson_object_id", "value": str(value)}
        if isinstance(value, (bytes, Binary)):
            return {
                "type": "bson_binary",
                "subtype": getattr(value, "subtype", 0),
                "value": base64.b64encode(value).decode("ascii"),
            }
        if isinstance(value, dict):
            # BSON embedded-document identity includes field order. A JSON object
            # sorted by a downstream hasher would otherwise collapse distinct IDs.
            return {
                "type": "bson_document",
                "fields": [[key, MongoConnector._identity(item)] for key, item in value.items()],
            }
        if isinstance(value, list):
            return {"type": "bson_array", "items": [MongoConnector._identity(item) for item in value]}
        if isinstance(value, (float, Decimal128)) or value is None:
            raise ConnectorError(
                "unsupported_key_semantics",
                "MongoDB floating, decimal or null identities require an explicit stable canonical key",
            )
        if isinstance(value, datetime):
            value = value.replace(tzinfo=UTC) if value.tzinfo is None else value
        return canonical_identity_key({"_id": value})["_id"]

    @staticmethod
    def _close_cursor(state: dict[str, Any]) -> None:
        try:
            state["cursor"].close()
        except Exception:  # noqa: BLE001 -- cleanup must not leak driver errors or obscure the scan failure.
            return

    def sample(self, resource: str, limit: int = 20) -> SnapshotPage:
        result = self.read_snapshot(resource, limit=limit)
        if result.next_cursor:
            state = self._cursors.pop(result.next_cursor, None)
            if state:
                self._close_cursor(state)
        return result.model_copy(update={"complete": False, "next_cursor": None})

    def close(self) -> None:
        for state in self._cursors.values():
            self._close_cursor(state)
        self._cursors.clear()
        if self._client is not None:
            self._client.close()
        self._client = None
