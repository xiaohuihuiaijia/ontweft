"""Read configured nodes and directed relationships using stable business keys."""

from __future__ import annotations

import hashlib
import json
import math
import uuid
from typing import Any
from urllib.parse import urlsplit

from pydantic import BaseModel, Field, SecretStr, model_validator

from ontology.connectors.base import (
    ConnectionCheck,
    ConnectorBase,
    ConnectorCapabilities,
    ConnectorError,
    ForeignKey,
    ResourceSchema,
    SnapshotPage,
    SourceRecord,
    validate_limit,
)
from ontology.connectors.policy import NetworkPolicy


class Neo4jRelationship(BaseModel):
    relationship_type: str = Field(min_length=1)
    source_label: str = Field(min_length=1)
    target_label: str = Field(min_length=1)
    key_fields: list[str] = Field(min_length=1)

    @model_validator(mode="after")
    def unique_keys(self):
        if any(not key for key in self.key_fields) or len(set(self.key_fields)) != len(self.key_fields):
            raise ValueError("Relationship keys must be nonempty and distinct")
        return self


class Neo4jConfig(BaseModel):
    uri: str
    database: str = "neo4j"
    username: str
    password: SecretStr = Field(repr=False)
    label_keys: dict[str, str] = Field(default_factory=dict)
    relationships: dict[str, Neo4jRelationship] = Field(default_factory=dict)
    timeout_seconds: int = Field(default=15, ge=1, le=300)

    @model_validator(mode="after")
    def endpoint_keys(self):
        if any(not label or not key for label, key in self.label_keys.items()):
            raise ValueError("Node labels and business keys must be nonempty")
        if set(self.label_keys) & set(self.relationships):
            raise ValueError("Resource IDs must be distinct")
        for resource, relation in self.relationships.items():
            if not resource or not {relation.source_label, relation.target_label}.issubset(self.label_keys):
                raise ValueError("Relationship endpoints require configured business keys")
        return self


class Neo4jConnector(ConnectorBase):
    capabilities = ConnectorCapabilities(
        notes=[
            "Configured nodes and directed relationships; each resource and endpoint requires stable unique business keys",
            "Neo4j 5.13+ valueType validation requires each key column to contain only strings or only integers",
            "Internal node/relationship IDs are never identities; parallel relationships need their own business keys",
            "Concurrent writes can affect paginated reads; not an atomic graph snapshot",
        ]
    )

    def __init__(self, config: Neo4jConfig, *, network_policy: NetworkPolicy):
        self.config, self.policy = config, network_policy
        self._driver: Any = None
        self._cursors: dict[str, tuple[str, list[Any], str, str]] = {}

    def _get_driver(self):
        if self._driver is None:
            parsed = urlsplit(self.config.uri)
            if (
                parsed.scheme not in {"bolt", "bolt+s"}
                or parsed.username
                or parsed.password
                or not parsed.hostname
            ):
                raise ConnectorError(
                    "network_denied",
                    "Use an explicitly allowed direct bolt URI; routed connections are disabled",
                )
            self.policy.check_host(parsed.hostname, parsed.port or 7687)
            try:
                from neo4j import GraphDatabase

                self._driver = GraphDatabase.driver(
                    self.config.uri,
                    auth=(self.config.username, self.config.password.get_secret_value()),
                    connection_timeout=self.config.timeout_seconds,
                )
            except ImportError:
                raise ConnectorError("driver_unavailable", "Neo4j driver is not installed") from None
            except Exception:  # noqa: BLE001 -- public adapter boundary redacts untrusted driver errors.
                raise ConnectorError("connection_failed", "Graph connection failed", retryable=True) from None
        return self._driver

    @staticmethod
    def _name(value: str) -> str:
        return "`" + value.replace("`", "``") + "`"

    def _query(self, query: str, **parameters: Any) -> list[dict]:
        try:
            from neo4j import READ_ACCESS, Query

            with self._get_driver().session(
                database=self.config.database, default_access_mode=READ_ACCESS
            ) as session:
                return session.run(Query(query, timeout=self.config.timeout_seconds), parameters).data()
        except ConnectorError:
            raise
        except Exception:  # noqa: BLE001 -- public adapter boundary redacts untrusted driver errors.
            raise ConnectorError("source_read_failed", "Graph source read failed", retryable=True) from None

    def test_connection(self) -> ConnectionCheck:
        try:
            self._query("RETURN 1 AS connected")
            return ConnectionCheck(ok=True)
        except ConnectorError as exc:
            return ConnectionCheck(ok=False, code=exc.code, message=str(exc))

    def discover(self) -> list[ResourceSchema]:
        labels = {row["label"] for row in self._query("CALL db.labels() YIELD label RETURN label")}
        if not set(self.config.label_keys).issubset(labels):
            raise ConnectorError("incomplete_discovery", "A configured node label is missing or inaccessible")
        resources = [
            ResourceSchema(resource_id=label, name=label, kind="graph_nodes", primary_key=[key])
            for label, key in self.config.label_keys.items()
        ]
        if self.config.relationships:
            types = {
                row["relationshipType"]
                for row in self._query(
                    "CALL db.relationshipTypes() YIELD relationshipType RETURN relationshipType"
                )
            }
            if any(r.relationship_type not in types for r in self.config.relationships.values()):
                raise ConnectorError(
                    "incomplete_discovery", "A configured relationship type is missing or inaccessible"
                )
        for resource, relation in self.config.relationships.items():
            resources.append(
                ResourceSchema(
                    resource_id=resource,
                    name=relation.relationship_type,
                    kind="graph_relationships",
                    primary_key=relation.key_fields,
                    foreign_keys=[
                        ForeignKey(
                            fields=[f"source.key.{self.config.label_keys[relation.source_label]}"],
                            target_resource=relation.source_label,
                            target_fields=[self.config.label_keys[relation.source_label]],
                        ),
                        ForeignKey(
                            fields=[f"target.key.{self.config.label_keys[relation.target_label]}"],
                            target_resource=relation.target_label,
                            target_fields=[self.config.label_keys[relation.target_label]],
                        ),
                    ],
                    metadata={
                        "directed": True,
                        "source_label": relation.source_label,
                        "target_label": relation.target_label,
                    },
                )
            )
        return resources

    def _scope(self, resource: str) -> tuple[str, str, list[str], Neo4jRelationship | None]:
        relation = self.config.relationships.get(resource)
        if relation:
            pattern = f"(s:{self._name(relation.source_label)})-[r:{self._name(relation.relationship_type)}]->(t:{self._name(relation.target_label)})"
            return pattern, "r", relation.key_fields, relation
        key = self.config.label_keys.get(resource)
        if key:
            return f"(n:{self._name(resource)})", "n", [key], None
        raise ConnectorError("unknown_resource", "Configure the graph resource and its stable business keys")

    def _validate_keys(self, pattern: str, alias: str, keys: list[str]) -> None:
        projections = ", ".join(f"{alias}.{self._name(key)} AS k{i}" for i, key in enumerate(keys))
        nulls = " OR ".join(f"k{i} IS NULL" for i in range(len(keys)))
        invalid = self._query(
            f"MATCH {pattern} WITH {projections}, count(*) AS c WHERE {nulls} OR c>1 RETURN c LIMIT 1"
        )
        if invalid:
            raise ConnectorError("invalid_primary_key", "Graph business key is missing or non-unique")
        # Cypher can order heterogeneous property types, but a keyset contract
        # should not silently depend on that order or accept lossy numeric IDs.
        types = self._query(
            "MATCH "
            + pattern
            + " RETURN DISTINCT "
            + ", ".join(f"valueType({alias}.{self._name(key)}) AS k{i}" for i, key in enumerate(keys))
        )
        for i in range(len(keys)):
            values = {row[f"k{i}"] for row in types}
            if len(values) > 1 or not values.issubset({"STRING NOT NULL", "INTEGER NOT NULL"}):
                raise ConnectorError(
                    "unsupported_key_type", "Each graph key column must contain only strings or only integers"
                )

    @staticmethod
    def _value(value: Any) -> Any:
        """Preserve Neo4j precision and spatial/temporal type in a JSON-safe envelope."""
        from neo4j.spatial import Point
        from neo4j.time import Date, DateTime, Duration, Time

        if isinstance(value, (Date, DateTime, Duration, Time)):
            return {"$neo4j_type": type(value).__name__, "value": value.iso_format()}
        if isinstance(value, Point):
            return {"$neo4j_type": type(value).__name__, "srid": value.srid, "coordinates": list(value)}
        if isinstance(value, dict):
            return {key: Neo4jConnector._value(item) for key, item in value.items()}
        if isinstance(value, (list, tuple)):
            return [Neo4jConnector._value(item) for item in value]
        if isinstance(value, bytearray):
            return bytes(value)
        if isinstance(value, float) and not math.isfinite(value):
            return {"$neo4j_type": "Float", "value": str(value)}
        if value is None or isinstance(value, (str, int, float, bool, bytes)):
            return value
        raise ConnectorError(
            "unsupported_value_type", "Graph property type cannot be represented without loss"
        )

    @staticmethod
    def _record_key(properties: dict, keys: list[str]) -> dict:
        result = {key: properties.get(key) for key in keys}
        if any(type(value) not in {str, int} for value in result.values()):
            raise ConnectorError(
                "invalid_primary_key", "Graph key changed or has an unsupported type during enumeration"
            )
        return result

    def read_snapshot(self, resource: str, *, cursor: str | None = None, limit: int = 1000) -> SnapshotPage:
        validate_limit(limit)
        pattern, alias, keys, relation = self._scope(resource)
        fingerprint = hashlib.sha256(
            json.dumps(
                {"pattern": pattern, "keys": keys, "label_keys": self.config.label_keys}, sort_keys=True
            ).encode()
        ).hexdigest()
        snapshot, last = str(uuid.uuid4()), None
        if cursor:
            state = self._cursors.get(cursor)
            if state is None or state[0] != resource or state[3] != fingerprint:
                raise ConnectorError("invalid_cursor", "Graph cursor is invalid or expired")
            _, last, snapshot, _ = state
        else:
            if relation:
                for endpoint in {relation.source_label, relation.target_label}:
                    self._validate_keys(
                        f"(n:{self._name(endpoint)})", "n", [self.config.label_keys[endpoint]]
                    )
            self._validate_keys(pattern, alias, keys)
        fields = [f"{alias}.{self._name(key)}" for key in keys]
        parameters: dict[str, Any] = {"limit": limit + 1}
        where = ""
        if last is not None:
            terms = []
            for i, field in enumerate(fields):
                comparisons = [f"{fields[j]}=$key{j}" for j in range(i)] + [f"{field}>$key{i}"]
                terms.append("(" + " AND ".join(comparisons) + ")")
                parameters[f"key{i}"] = last[i]
            where = " WHERE " + " OR ".join(terms)
        projection = f"properties({alias}) AS properties"
        if relation:
            projection += f", s.{self._name(self.config.label_keys[relation.source_label])} AS source_key, t.{self._name(self.config.label_keys[relation.target_label])} AS target_key"
        rows = self._query(
            f"MATCH {pattern}{where} RETURN {projection} ORDER BY {', '.join(fields)} LIMIT $limit",
            **parameters,
        )
        records = []
        for row in rows[:limit]:
            key = self._record_key(row["properties"], keys)
            values = self._value(row["properties"])
            if relation:
                values = {
                    "relationship_type": relation.relationship_type,
                    "source": {
                        "resource_id": relation.source_label,
                        "key": self._record_key(
                            {self.config.label_keys[relation.source_label]: row["source_key"]},
                            [self.config.label_keys[relation.source_label]],
                        ),
                    },
                    "target": {
                        "resource_id": relation.target_label,
                        "key": self._record_key(
                            {self.config.label_keys[relation.target_label]: row["target_key"]},
                            [self.config.label_keys[relation.target_label]],
                        ),
                    },
                    "properties": values,
                }
            records.append(SourceRecord(key=key, values=values))
        token = None
        if len(rows) > limit:
            token = str(uuid.uuid4())
            self._cursors[token] = (resource, [records[-1].key[key] for key in keys], snapshot, fingerprint)
        if cursor:
            self._cursors.pop(cursor, None)
        return SnapshotPage(
            resource_id=resource,
            snapshot_id=snapshot,
            next_cursor=token,
            complete=token is None,
            records=records,
            schema_fingerprint=fingerprint,
            warnings=[
                "Configured graph scope only; concurrent writes may affect enumeration, so completion is not an authoritative deletion inventory"
            ],
        )

    def sample(self, resource: str, limit: int = 20) -> SnapshotPage:
        page = self.read_snapshot(resource, limit=limit)
        if page.next_cursor:
            self._cursors.pop(page.next_cursor, None)
        return page.model_copy(update={"complete": False, "next_cursor": None})

    def close(self) -> None:
        if self._driver is not None:
            self._driver.close()
        self._driver = None
        self._cursors.clear()
