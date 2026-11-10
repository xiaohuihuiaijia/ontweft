"""Read-only relational discovery and transaction-bound snapshot pagination.

Page cursors are one-use capabilities tied to this connector instance. They are
not durable CDC offsets and intentionally fail after process/connection loss.
"""

from __future__ import annotations

import hashlib
import logging
import sqlite3
import uuid
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field, SecretStr

from ontology.connectors.base import (
    ConnectionCheck,
    ConnectorBase,
    ConnectorCapabilities,
    ConnectorError,
    FieldSchema,
    ForeignKey,
    ResourceSchema,
    SnapshotPage,
    SourceRecord,
    canonical_identity_key,
    validate_limit,
)
from ontology.connectors.policy import NetworkPolicy


class SQLConfig(BaseModel):
    driver: Literal["sqlite", "postgres", "mysql"]
    database: str
    host: str | None = None
    port: int | None = None
    username: str | None = None
    password: SecretStr | None = Field(default=None, repr=False)
    schemas: list[str] = Field(default_factory=list)
    timeout_seconds: int = Field(default=15, ge=1, le=300)


class SQLConnector(ConnectorBase):
    capabilities = ConnectorCapabilities(
        consistent_snapshot=True,
        notes=[
            "Snapshot cursors expire when this connection closes; restart the snapshot",
            "CDC is supplied by Debezium, not this read adapter",
            "Tables without a declared primary key allow sampling only",
        ],
    )

    def __init__(self, config: SQLConfig, *, network_policy: NetworkPolicy | None = None):
        self.config = config
        self._policy = network_policy
        self._connection: Any = None
        self._resources: dict[str, ResourceSchema] = {}
        self._pages: dict[str, dict[str, Any]] = {}
        self._active = False
        self._stream_cursor: Any = None

    def _connect(self) -> Any:
        if self._connection is not None:
            return self._connection
        try:
            if self.config.driver == "sqlite":
                path = Path(self.config.database).resolve(strict=True)
                conn = sqlite3.connect(
                    path.as_uri() + "?mode=ro",
                    uri=True,
                    timeout=self.config.timeout_seconds,
                    isolation_level=None,
                )
                conn.execute("PRAGMA query_only = ON")
            else:
                if not self._policy or not self.config.host:
                    raise ConnectorError("network_denied", "An explicit source host policy is required")
                self._policy.check_host(self.config.host, self.config.port)
                password = self.config.password.get_secret_value() if self.config.password else None
                if self.config.driver == "postgres":
                    import pg8000.dbapi

                    conn = pg8000.dbapi.connect(
                        host=self.config.host,
                        port=self.config.port or 5432,
                        database=self.config.database,
                        user=self.config.username,
                        password=password,
                        timeout=self.config.timeout_seconds,
                    )
                    conn.autocommit = True
                else:
                    import pymysql

                    conn = pymysql.connect(
                        host=self.config.host,
                        port=self.config.port or 3306,
                        database=self.config.database,
                        user=self.config.username,
                        password=password,
                        connect_timeout=self.config.timeout_seconds,
                        read_timeout=self.config.timeout_seconds,
                        write_timeout=self.config.timeout_seconds,
                        autocommit=True,
                        charset="utf8mb4",
                    )
            self._connection = conn
            return conn
        except ConnectorError:
            raise
        except ImportError:
            raise ConnectorError(
                "driver_unavailable", "The selected database driver is not installed"
            ) from None
        except Exception:  # noqa: BLE001 -- public adapter boundary redacts untrusted driver errors.
            raise ConnectorError("connection_failed", "Source connection failed", retryable=True) from None

    def _rows(self, sql: str, params: tuple = ()) -> list[dict]:
        cur = self._connect().cursor()
        try:
            if params:
                cur.execute(sql, params)
            else:
                cur.execute(sql)
            names = [c[0] for c in cur.description or []]
            return [dict(zip(names, row)) for row in cur.fetchall()]
        except Exception:  # noqa: BLE001 -- public adapter boundary redacts untrusted driver errors.
            raise ConnectorError("source_read_failed", "Source metadata could not be read") from None
        finally:
            cur.close()

    def test_connection(self) -> ConnectionCheck:
        try:
            self._rows("SELECT 1")
            return ConnectionCheck(ok=True)
        except ConnectorError as exc:
            return ConnectionCheck(ok=False, code=exc.code, message=str(exc))

    def _quote(self, name: str) -> str:
        q = "`" if self.config.driver == "mysql" else '"'
        return q + name.replace(q, q + q) + q

    def _metadata(self, sql: str, params: tuple = ()) -> list[dict]:
        # MySQL exposes information_schema headings in uppercase. Normalize only
        # metadata headings; source table column names must remain unchanged.
        return [{key.lower(): value for key, value in row.items()} for row in self._rows(sql, params)]

    def discover(self) -> list[ResourceSchema]:
        if self._active:
            raise ConnectorError("snapshot_active", "Finish or close the active snapshot before discovery")
        resources: list[ResourceSchema] = []
        if self.config.driver == "sqlite":
            tables = self._metadata(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
            )
            for table in tables:
                name = table["name"]
                cols = self._metadata(f"PRAGMA table_info({self._quote(name)})")
                collations = {}
                for index in self._metadata(f"PRAGMA index_list({self._quote(name)})"):
                    if index.get("origin") == "pk":
                        collations.update(
                            {
                                row["name"]: row["coll"]
                                for row in self._metadata(f"PRAGMA index_xinfo({self._quote(index['name'])})")
                                if row.get("key") and row.get("name")
                            }
                        )
                foreign: dict[int, list[dict]] = {}
                for row in self._metadata(f"PRAGMA foreign_key_list({self._quote(name)})"):
                    foreign.setdefault(row["id"], []).append(row)
                fks = []
                for rows in foreign.values():
                    rows.sort(key=lambda row: row["seq"])
                    fks.append(
                        ForeignKey(
                            fields=[r["from"] for r in rows],
                            target_resource="main." + rows[0]["table"],
                            target_fields=[r["to"] or "" for r in rows],
                        )
                    )
                resources.append(
                    ResourceSchema(
                        resource_id="main." + name,
                        name=name,
                        namespace="main",
                        fields=[
                            FieldSchema(
                                name=c["name"],
                                data_type=c["type"],
                                nullable=not bool(c["notnull"] or c["pk"]),
                                ordinal=c["cid"],
                                metadata={"collation_name": collations[c["name"]]}
                                if c["name"] in collations
                                else {},
                            )
                            for c in cols
                        ],
                        primary_key=[c["name"] for c in sorted(cols, key=lambda c: c["pk"]) if c["pk"]],
                        foreign_keys=fks,
                    )
                )
        else:
            placeholder = "%s"
            schemas = self.config.schemas or (
                ["public"] if self.config.driver == "postgres" else [self.config.database]
            )
            for schema in schemas:
                tables = self._metadata(
                    "SELECT table_name"
                    + (",engine" if self.config.driver == "mysql" else "")
                    + " FROM information_schema.tables WHERE table_schema="
                    + placeholder
                    + " AND table_type='BASE TABLE' ORDER BY table_name",
                    (schema,),
                )
                for table in tables:
                    name = table["table_name"]
                    cols = self._metadata(
                        "SELECT column_name,data_type,is_nullable,ordinal_position,numeric_precision,numeric_scale,character_maximum_length,datetime_precision,collation_name"
                        + (",udt_name" if self.config.driver == "postgres" else "")
                        + " FROM information_schema.columns "
                        "WHERE table_schema=%s AND table_name=%s ORDER BY ordinal_position",
                        (schema, name),
                    )
                    if not cols:
                        raise ConnectorError(
                            "incomplete_discovery", "A listed table has no visible column metadata"
                        )
                    if self.config.driver == "postgres":
                        collations = {
                            row["column_name"]: row
                            for row in self._metadata(
                                "SELECT a.attname AS column_name,c.collname AS collation_name,c.collisdeterministic AS collation_deterministic "
                                "FROM pg_catalog.pg_attribute a JOIN pg_catalog.pg_class t ON t.oid=a.attrelid "
                                "JOIN pg_catalog.pg_namespace n ON n.oid=t.relnamespace "
                                "LEFT JOIN pg_catalog.pg_collation c ON c.oid=a.attcollation "
                                "WHERE n.nspname=%s AND t.relname=%s AND a.attnum>0 AND NOT a.attisdropped",
                                (schema, name),
                            )
                        }
                        for column in cols:
                            column.update(
                                {
                                    k: v
                                    for k, v in collations.get(column["column_name"], {}).items()
                                    if k != "column_name"
                                }
                            )
                        # information_schema.table_constraints omits constraints
                        # for SELECT-only accounts. Only inspect already visible,
                        # explicitly scoped tables, preserving conkey order.
                        keys = self._metadata(
                            "SELECT a.attname AS column_name,k.ordinality AS ordinal_position "
                            "FROM pg_catalog.pg_constraint c "
                            "JOIN pg_catalog.pg_class t ON t.oid=c.conrelid "
                            "JOIN pg_catalog.pg_namespace n ON n.oid=t.relnamespace "
                            "CROSS JOIN LATERAL unnest(c.conkey) WITH ORDINALITY k(attnum,ordinality) "
                            "JOIN pg_catalog.pg_attribute a ON a.attrelid=t.oid AND a.attnum=k.attnum "
                            "WHERE c.contype='p' AND n.nspname=%s AND t.relname=%s ORDER BY k.ordinality",
                            (schema, name),
                        )
                        fk_rows = self._metadata(
                            "SELECT c.conname AS constraint_name,a.attname AS column_name,k.ordinality AS ordinal_position, "
                            "rn.nspname AS target_schema,rt.relname AS target_table,ra.attname AS target_column "
                            "FROM pg_catalog.pg_constraint c "
                            "JOIN pg_catalog.pg_class t ON t.oid=c.conrelid "
                            "JOIN pg_catalog.pg_namespace n ON n.oid=t.relnamespace "
                            "JOIN pg_catalog.pg_class rt ON rt.oid=c.confrelid "
                            "JOIN pg_catalog.pg_namespace rn ON rn.oid=rt.relnamespace "
                            "CROSS JOIN LATERAL unnest(c.conkey,c.confkey) WITH ORDINALITY k(attnum,target_attnum,ordinality) "
                            "JOIN pg_catalog.pg_attribute a ON a.attrelid=t.oid AND a.attnum=k.attnum "
                            "JOIN pg_catalog.pg_attribute ra ON ra.attrelid=rt.oid AND ra.attnum=k.target_attnum "
                            "WHERE c.contype='f' AND n.nspname=%s AND t.relname=%s",
                            (schema, name),
                        )
                    else:
                        collations = {
                            row["collation_name"]: row["pad_attribute"]
                            for row in self._metadata(
                                "SELECT collation_name,pad_attribute FROM information_schema.collations"
                            )
                        }
                        for column in cols:
                            column["pad_attribute"] = collations.get(column.get("collation_name"))
                        keys = self._metadata(
                            "SELECT k.column_name,k.ordinal_position FROM information_schema.table_constraints t "
                            "JOIN information_schema.key_column_usage k ON t.constraint_name=k.constraint_name "
                            "AND t.table_schema=k.table_schema AND t.table_name=k.table_name "
                            "WHERE t.constraint_type='PRIMARY KEY' AND t.table_schema=%s AND t.table_name=%s "
                            "ORDER BY k.ordinal_position",
                            (schema, name),
                        )
                        fk_rows = self._metadata(
                            "SELECT constraint_name,column_name,ordinal_position,referenced_table_schema AS target_schema,"
                            "referenced_table_name AS target_table,referenced_column_name AS target_column "
                            "FROM information_schema.key_column_usage WHERE table_schema=%s AND table_name=%s "
                            "AND referenced_table_name IS NOT NULL",
                            (schema, name),
                        )
                    groups: dict[str, list[dict]] = {}
                    for row in fk_rows:
                        groups.setdefault(row["constraint_name"], []).append(row)
                    fks = []
                    for rows in groups.values():
                        rows.sort(key=lambda row: row["ordinal_position"])
                        fks.append(
                            ForeignKey(
                                fields=[r["column_name"] for r in rows],
                                target_resource=f"{rows[0]['target_schema']}.{rows[0]['target_table']}",
                                target_fields=[r["target_column"] for r in rows],
                            )
                        )
                    resources.append(
                        ResourceSchema(
                            resource_id=f"{schema}.{name}",
                            namespace=schema,
                            name=name,
                            fields=[
                                FieldSchema(
                                    name=c["column_name"],
                                    data_type=c["data_type"],
                                    nullable=c["is_nullable"] == "YES",
                                    ordinal=c["ordinal_position"],
                                    metadata={
                                        key: c[key]
                                        for key in (
                                            "numeric_precision",
                                            "numeric_scale",
                                            "character_maximum_length",
                                            "datetime_precision",
                                            "udt_name",
                                            "collation_name",
                                            "collation_deterministic",
                                            "pad_attribute",
                                        )
                                        if c.get(key) is not None
                                    },
                                )
                                for c in cols
                            ],
                            primary_key=[k["column_name"] for k in keys],
                            foreign_keys=fks,
                            metadata={"storage_engine": table["engine"]}
                            if self.config.driver == "mysql"
                            else {},
                        )
                    )
        self._resources = {r.resource_id: r for r in resources}
        return resources

    def _resource(self, resource: str) -> ResourceSchema:
        if not self._resources:
            self.discover()
        if resource not in self._resources:
            raise ConnectorError("unknown_resource", "Resource is outside the discovered source scope")
        return self._resources[resource]

    def _table(self, resource: ResourceSchema) -> str:
        return ".".join(self._quote(s) for s in [resource.namespace, resource.name] if s)

    def sample(self, resource: str, limit: int = 20) -> SnapshotPage:
        validate_limit(limit, 1000)
        if self._active:
            raise ConnectorError("snapshot_active", "Sampling cannot share an active snapshot")
        schema = self._resource(resource)
        rows = self._rows(f"SELECT * FROM {self._table(schema)} LIMIT {int(limit)}")
        return SnapshotPage(
            records=[self._record(schema, r) for r in rows],
            resource_id=resource,
            snapshot_id=str(uuid.uuid4()),
            complete=False,
            warnings=["Sample only; never use this page to infer deletions or uniqueness"],
        )

    @staticmethod
    def _record(schema: ResourceSchema, values: dict) -> SourceRecord:
        key = {key: values[key] for key in schema.primary_key}
        return SourceRecord(
            key=key,
            identity_key=canonical_identity_key(key)
            if all(value is not None for value in key.values())
            else None,
            values=values,
        )

    def _validate_key_semantics(self, schema: ResourceSchema) -> None:
        for field in schema.fields:
            if field.name not in schema.primary_key:
                continue
            meta = field.metadata
            if self.config.driver == "postgres":
                if meta.get("udt_name") == "citext" or meta.get("collation_deterministic") is False:
                    raise ConnectorError(
                        "unsupported_key_semantics",
                        "Case-insensitive or nondeterministic text keys require an explicit stable canonical identity",
                    )
            elif (
                self.config.driver == "mysql"
                and meta.get("collation_name")
                and (not meta["collation_name"].endswith("_bin") or meta.get("pad_attribute") != "NO PAD")
            ):
                raise ConnectorError(
                    "unsupported_key_semantics",
                    "MySQL text keys require binary NO PAD collation or an explicit stable canonical identity",
                )
            elif self.config.driver == "sqlite" and meta.get("collation_name", "BINARY") != "BINARY":
                raise ConnectorError(
                    "unsupported_key_semantics",
                    "SQLite non-binary primary keys require an explicit stable canonical identity",
                )

    def read_snapshot(self, resource: str, *, cursor: str | None = None, limit: int = 1000) -> SnapshotPage:
        validate_limit(limit)
        if cursor is not None:
            state = self._pages.pop(cursor, None)
            if state is None:
                raise ConnectorError(
                    "invalid_cursor", "Snapshot cursor is expired or has already been consumed"
                )
            if state["schema"].resource_id != resource:
                self._pages[cursor] = state
                raise ConnectorError("invalid_cursor", "Cursor belongs to another resource")
        else:
            if self._active:
                raise ConnectorError("snapshot_active", "Only one transaction snapshot may run per connector")
            schema = self._resource(resource)
            if not schema.primary_key:
                raise ConnectorError(
                    "missing_primary_key", "Configure a stable key before importing this table"
                )
            self._validate_key_semantics(schema)
            if self.config.driver == "mysql" and schema.metadata.get("storage_engine") != "InnoDB":
                raise ConnectorError(
                    "unsupported_snapshot", "Consistent MySQL snapshots require an InnoDB table"
                )
            conn = self._connect()
            cur = conn.cursor()
            server_cursor = None
            try:
                if self.config.driver == "postgres":
                    cur.execute("BEGIN TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
                    cur.execute(f"SET LOCAL statement_timeout = {self.config.timeout_seconds * 1000}")
                elif self.config.driver == "mysql":
                    cur.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")
                    cur.execute("START TRANSACTION WITH CONSISTENT SNAPSHOT, READ ONLY")
                else:
                    cur.execute("BEGIN")
                self._active = True
                order = ",".join(self._quote(k) for k in schema.primary_key)
                select = f"SELECT * FROM {self._table(schema)} ORDER BY {order}"
                if self.config.driver == "postgres":
                    server_cursor = self._quote("ontology_" + uuid.uuid4().hex)
                    cur.execute(f"DECLARE {server_cursor} NO SCROLL CURSOR FOR {select}")
                    cur.execute(f"FETCH FORWARD 0 FROM {server_cursor}")
                else:
                    if self.config.driver == "mysql":
                        from pymysql.cursors import SSCursor

                        cur.close()
                        cur = conn.cursor(SSCursor)
                        self._stream_cursor = cur
                    cur.execute(select)
                columns = [c[0] for c in cur.description]
                if columns != [field.name for field in schema.fields]:
                    raise ConnectorError(
                        "schema_changed",
                        "Source columns changed since discovery; discover and review the mapping again",
                    )
                state = {
                    "cursor": cur,
                    "schema": schema,
                    "buffer": [],
                    "id": str(uuid.uuid4()),
                    "columns": columns,
                    "server_cursor": server_cursor,
                    "fingerprint": hashlib.sha256(schema.model_dump_json().encode()).hexdigest(),
                }
            except ConnectorError:
                self.close()
                raise
            except Exception:  # noqa: BLE001 -- public adapter boundary redacts untrusted driver errors.
                self.close()
                raise ConnectorError(
                    "snapshot_failed", "Could not start a consistent read-only snapshot"
                ) from None
        try:
            fetch_count = limit + 1 - len(state["buffer"])
            if state.get("server_cursor"):
                state["cursor"].execute(f"FETCH FORWARD {int(fetch_count)} FROM {state['server_cursor']}")
            rows = state["buffer"] + list(state["cursor"].fetchmany(fetch_count))
            has_more = len(rows) > limit
            state["buffer"] = rows[limit:]
            records = [
                self._record(state["schema"], dict(zip(state["columns"], row))) for row in rows[:limit]
            ]
            if any(any(v is None for v in record.key.values()) for record in records):
                raise ConnectorError("invalid_primary_key", "Snapshot contains a null primary-key component")
            next_cursor = str(uuid.uuid4()) if has_more else None
            if next_cursor:
                self._pages[next_cursor] = state
            else:
                if state.get("server_cursor"):
                    state["cursor"].execute(f"CLOSE {state['server_cursor']}")
                state["cursor"].close()
                self._connection.rollback()
                self._active = False
                self._stream_cursor = None
            return SnapshotPage(
                records=records,
                resource_id=resource,
                snapshot_id=state["id"],
                next_cursor=next_cursor,
                complete=not has_more,
                consistency="transaction",
                schema_fingerprint=state["fingerprint"],
            )
        except ConnectorError:
            self.close()
            raise
        except Exception:  # noqa: BLE001 -- public adapter boundary redacts untrusted driver errors.
            self.close()
            raise ConnectorError(
                "snapshot_lost", "Snapshot was interrupted; start a new snapshot", retryable=True
            ) from None

    def close(self) -> None:
        connection = self._connection
        active = self._active
        stream = self._stream_cursor
        self._stream_cursor = None
        self._connection = None
        self._pages.clear()
        self._active = False
        if connection is not None:
            # Closing an active stream aborts its server transaction. Issuing
            # ROLLBACK first makes PyMySQL drain the entire unbuffered result.
            if not active:
                try:
                    connection.rollback()
                except Exception:  # noqa: BLE001 -- drivers differ; never expose credential-bearing errors.
                    logging.getLogger(__name__).debug("Source rollback failed while closing connection")
            try:
                connection.close()
            except Exception:  # noqa: BLE001 -- cleanup cannot expose driver connection details.
                logging.getLogger(__name__).debug("Source connection close failed")
            finally:
                # PyMySQL's SSCursor.close drains unread rows. After aborting the
                # socket, detach its result so finalizers do not drain a dead socket.
                if stream is not None:
                    result = getattr(stream, "_result", None)
                    if result is not None:
                        result.unbuffered_active = False
                        result.connection = None
                    stream.connection = None
