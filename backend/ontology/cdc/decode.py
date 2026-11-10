"""Pure, loss-aware decoding of the pinned PostgreSQL Debezium Connect envelope.

No database, network, projection, replacement-row fetch or permission bypass lives here.
Unknown logical types and unrepresentable identities fail closed for review.
"""

from __future__ import annotations

import base64
import binascii
import math
import re
import struct
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from enum import StrEnum
from typing import Any
from uuid import UUID

from ontology.connectors.base import ConnectorError, canonical_identity_key


class CdcDecodeError(ValueError):
    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(message)


class ValueState(StrEnum):
    MISSING = "missing"
    NULL = "null"
    VALUE = "value"
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True)
class FieldValue:
    state: ValueState
    value: Any = None


@dataclass(frozen=True)
class ExactTemporal:
    """Preserve nanoseconds/out-of-Python-range time; never silently truncate."""

    logical_type: str
    raw_value: int | str
    unit: str


@dataclass(frozen=True)
class DecodedRow:
    fields: dict[str, FieldValue]

    def complete_values(self) -> dict[str, Any]:
        if any(value.state in {ValueState.MISSING, ValueState.UNAVAILABLE} for value in self.fields.values()):
            raise CdcDecodeError(
                "incomplete_row", "Missing or unavailable source fields require explicit merge policy"
            )
        return {key: complete_value(value) for key, value in self.fields.items()}


def complete_value(value):
    if isinstance(value, FieldValue):
        if value.state in {ValueState.MISSING, ValueState.UNAVAILABLE}:
            raise CdcDecodeError(
                "incomplete_row", "Missing or unavailable source fields require explicit merge policy"
            )
        return complete_value(value.value)
    if isinstance(value, dict):
        return {key: complete_value(item) for key, item in value.items()}
    if isinstance(value, list):
        return [complete_value(item) for item in value]
    if isinstance(value, tuple):
        return tuple(complete_value(item) for item in value)
    return value


@dataclass(frozen=True)
class TransactionBoundary:
    phase: str
    tx_id: int
    source_commit_us: int
    lsn: int
    event_count: int | None
    data_collections: dict[str, int]


@dataclass(frozen=True)
class TransactionMember:
    tx_id: int
    source_commit_us: int
    total_order: int
    data_collection_order: int


@dataclass(frozen=True)
class SnapshotNotification:
    status: str
    native_id: str
    connector_name: str
    resources: tuple[str, ...] | None = None
    collection: str | None = None
    row_count: int | None = None
    scan_status: str | None = None


@dataclass(frozen=True)
class DecodedChange:
    event_id: str
    kind: str
    operation: str | None
    key: dict[str, Any] | None
    identity_key: dict[str, Any] | None
    before: DecodedRow | None
    after: DecodedRow | None
    source_partition: dict[str, Any]
    source_position: dict[str, Any]
    resource_id: str | None
    snapshot: str | bool | None
    boundary: TransactionBoundary | None
    member: TransactionMember | None
    notification: SnapshotNotification | None = None


def fail(code="invalid_connect_value", message="Connect schema and payload do not agree"):
    raise CdcDecodeError(code, message)


def integer(value, *, minimum=None, maximum=None):
    if (
        type(value) is not int
        or minimum is not None
        and value < minimum
        or maximum is not None
        and value > maximum
    ):
        fail()
    return value


def binary(value):
    if not isinstance(value, str):
        fail()
    try:
        return base64.b64decode(value, validate=True)
    except (ValueError, binascii.Error):
        fail()


def exact_decimal(encoded, scale):
    scale = integer(scale, minimum=-100_000, maximum=100_000)
    raw = binary(encoded)
    if not raw or len(raw) > 65_536:
        fail()
    coefficient = int.from_bytes(raw, byteorder="big", signed=True)
    # Decimal(int) and tuple construction are exact even under a low precision context.
    parts = Decimal(coefficient).as_tuple()
    return Decimal((parts.sign, parts.digits, -scale))


def temporal(name, value):
    if name in {"io.debezium.time.ZonedTimestamp", "io.debezium.time.ZonedTime"}:
        if not isinstance(value, str):
            fail()
        fraction = re.search(r"[.,](\d+)", value)
        if fraction and any(char != "0" for char in fraction[1][6:]):
            return ExactTemporal(name, value, "iso8601")
        try:
            parsed = (
                datetime.fromisoformat(value)
                if name.endswith("ZonedTimestamp")
                else time.fromisoformat(value)
            )
            if parsed.tzinfo is None:
                fail()
            return parsed
        except ValueError:
            fail()
    value = integer(value)
    if name in {"io.debezium.time.Date", "org.apache.kafka.connect.data.Date"}:
        try:
            return date(1970, 1, 1) + timedelta(days=value)
        except (OverflowError, ValueError):
            return ExactTemporal(name, value, "days")
    unit = "ns" if "Nano" in name else "us" if "Micro" in name else "ms"
    if unit == "ns" and value % 1000:
        return ExactTemporal(name, value, unit)
    micros = value // 1000 if unit == "ns" else value if unit == "us" else value * 1000
    if name.endswith("Time") and not name.endswith("Timestamp"):
        if not 0 <= micros < 86_400_000_000:
            return ExactTemporal(name, value, unit)
        seconds, microsecond = divmod(micros, 1_000_000)
        hour, seconds = divmod(seconds, 3600)
        minute, second = divmod(seconds, 60)
        return time(hour, minute, second, microsecond)
    try:
        # Debezium local Timestamp deliberately has no timezone; Kafka Timestamp is an instant.
        epoch = (
            datetime(1970, 1, 1, tzinfo=UTC)
            if name == "org.apache.kafka.connect.data.Timestamp"
            else datetime(1970, 1, 1)
        )
        return epoch + timedelta(microseconds=micros)
    except (OverflowError, ValueError):
        return ExactTemporal(name, value, unit)


TEMPORAL_TYPES = {
    "io.debezium.time.Date",
    "io.debezium.time.Time",
    "io.debezium.time.MicroTime",
    "io.debezium.time.NanoTime",
    "io.debezium.time.Timestamp",
    "io.debezium.time.MicroTimestamp",
    "io.debezium.time.NanoTimestamp",
    "io.debezium.time.ZonedTimestamp",
    "io.debezium.time.ZonedTime",
    "org.apache.kafka.connect.data.Date",
    "org.apache.kafka.connect.data.Time",
    "org.apache.kafka.connect.data.Timestamp",
}


class ConnectDecoder:
    def __init__(self, *, unavailable_value_placeholder="__debezium_unavailable_value", max_values=100_000):
        self.placeholder = unavailable_value_placeholder
        self.remaining = max_values

    def decode(self, schema, value, *, depth=0):
        self.remaining -= 1
        if self.remaining < 0 or depth > 64:
            fail("decode_budget", "Connect decoding budget exceeded")
        if value is None:
            return None
        if not isinstance(schema, dict):
            fail("missing_schema", "Non-null Connect value requires an explicit schema")
        kind, name = schema.get("type"), schema.get("name", "")
        if name == "org.apache.kafka.connect.data.Decimal":
            if kind != "bytes":
                fail()
            scale = schema.get("parameters", {}).get("scale")
            if not isinstance(scale, str) or not re.fullmatch(r"-?\d{1,6}", scale):
                fail()
            return exact_decimal(value, int(scale))
        if name == "io.debezium.data.VariableScaleDecimal":
            if kind != "struct" or not isinstance(value, dict) or set(value) != {"scale", "value"}:
                fail()
            return exact_decimal(value["value"], value["scale"])
        if name in TEMPORAL_TYPES:
            expected_kind = (
                "string" if "Zoned" in name else "int32" if name.endswith((".Date", ".Time")) else "int64"
            )
            if kind != expected_kind:
                fail()
            return temporal(name, value)
        if name == "io.debezium.data.Uuid":
            try:
                if kind != "string" or not isinstance(value, str):
                    fail()
                return UUID(value)
            except ValueError:
                fail()
        if name.startswith(
            ("io.debezium.time.", "io.debezium.data.", "org.apache.kafka.connect.data.")
        ) and name not in {"io.debezium.data.Json", "io.debezium.data.Enum", "io.debezium.data.EnumSet"}:
            fail("unsupported_logical_type", "Connect logical type needs an explicit lossless decoder")
        if kind in {"int8", "int16", "int32", "int64"}:
            bits = int(kind[3:])
            return integer(value, minimum=-(2 ** (bits - 1)), maximum=2 ** (bits - 1) - 1)
        if kind in {"float", "double", "float32", "float64"}:
            if type(value) not in {int, float}:
                fail()
            try:
                if not math.isfinite(value):
                    fail()
                return (
                    struct.unpack("!f", struct.pack("!f", value))[0]
                    if kind in {"float", "float32"}
                    else float(value)
                )
            except OverflowError:
                fail()
        if kind == "boolean":
            if type(value) is not bool:
                fail()
            return value
        if kind == "string":
            if not isinstance(value, str):
                fail()
            return value
        if kind == "bytes":
            return binary(value)
        if kind == "array":
            if not isinstance(value, list):
                fail()
            return [self.decode(schema.get("items"), item, depth=depth + 1) for item in value]
        if kind == "map":
            key_schema, value_schema = schema.get("keys"), schema.get("values")
            if isinstance(value, dict) and key_schema and key_schema.get("type") == "string":
                return {key: self.decode(value_schema, item, depth=depth + 1) for key, item in value.items()}
            if isinstance(value, list):
                pairs = []
                for item in value:
                    if not isinstance(item, list) or len(item) != 2:
                        fail()
                    pairs.append(
                        (
                            self.decode(key_schema, item[0], depth=depth + 1),
                            self.decode(value_schema, item[1], depth=depth + 1),
                        )
                    )
                return pairs  # typed, possibly unhashable keys cannot be collapsed into a dict
            fail()
        if kind == "struct":
            return self.row(schema, value, depth=depth + 1, check_unavailable=False).fields
        fail("unsupported_connect_type", "Connect type is not supported")

    def row(self, schema, payload, *, depth=0, check_unavailable=True):
        if not isinstance(schema, dict) or schema.get("type") != "struct" or not isinstance(payload, dict):
            fail()
        definitions = schema.get("fields")
        if not isinstance(definitions, list):
            fail()
        names = [item.get("field") for item in definitions if isinstance(item, dict)]
        if (
            len(names) != len(definitions)
            or any(not isinstance(name, str) for name in names)
            or len(set(names)) != len(names)
        ):
            fail()
        if set(payload) - set(names):
            fail("schema_drift", "Source payload includes fields absent from its own Connect schema")
        fields = {}
        for definition in definitions:
            name = definition["field"]
            if name not in payload:
                fields[name] = FieldValue(ValueState.MISSING)
            elif payload[name] is None:
                fields[name] = FieldValue(ValueState.NULL)
            else:
                value = self.decode(definition, payload[name], depth=depth + 1)
                unavailable = (
                    check_unavailable
                    and self.placeholder is not None
                    and (
                        isinstance(value, str)
                        and value == self.placeholder
                        or isinstance(value, bytes)
                        and value == self.placeholder.encode("utf-8")
                    )
                )
                fields[name] = FieldValue(
                    ValueState.UNAVAILABLE if unavailable else ValueState.VALUE,
                    None if unavailable else value,
                )
        return DecodedRow(fields)


def decode_pg_event(
    raw: dict, *, unavailable_value_placeholder="__debezium_unavailable_value"
) -> DecodedChange:
    if raw.get("connector") != "postgresql":
        fail("unsupported_connector", "This decoder only accepts the pinned PostgreSQL envelope")
    decoder = ConnectDecoder(unavailable_value_placeholder=unavailable_value_placeholder)
    key_part, value_part = raw.get("key"), raw.get("value")
    if not isinstance(key_part, dict) or not isinstance(value_part, dict):
        fail()
    kind, operation = raw.get("event_kind"), raw.get("operation")
    offset, partition = raw.get("source_offset"), raw.get("source_partition")
    if not isinstance(offset, dict) or not isinstance(partition, dict):
        fail()
    position = {
        key: offset[key] for key in ("lsn", "txId", "ts_usec", "messageType", "snapshot") if key in offset
    }
    if (
        kind == "control"
        and (value_part.get("schema") or {}).get("name") == "io.debezium.connector.common.Notification"
    ):
        notification = decode_snapshot_notification(raw, decoder)
        if "lsn" in offset:
            integer(offset["lsn"], minimum=0)
        elif notification.status not in {"STARTED", "ABORTED"}:
            fail(
                "missing_snapshot_position",
                "A native progress/completion notification requires its actual source position",
            )
        return DecodedChange(
            raw.get("event_id", ""),
            kind,
            operation,
            None,
            None,
            None,
            None,
            partition,
            position,
            None,
            None,
            None,
            None,
            notification,
        )
    lsn = integer(offset.get("lsn"), minimum=0)
    key, identity = None, None
    if key_part.get("payload") is not None and kind in {"data", "tombstone"}:
        key = decoder.row(
            key_part.get("schema"), key_part["payload"], check_unavailable=False
        ).complete_values()
        try:
            identity = canonical_identity_key(key)
        except ConnectorError:
            fail(
                "unsupported_key_semantics",
                "Source key cannot be represented by the stable typed identity codec",
            )
    if kind in {"data", "tombstone"} and operation != "t" and not key:
        fail("missing_source_key", "A source row without a stable key cannot be projected")
    payload = value_part.get("payload")
    before = after = boundary = member = resource_id = None
    if kind == "transaction":
        if not isinstance(payload, dict) or raw.get("transaction") != payload:
            fail("invalid_transaction", "Transaction boundary payload is inconsistent")
        expected = {"BEGIN": "BEGIN", "END": "COMMIT"}.get(payload.get("status"))
        if expected is None or offset.get("messageType") != expected:
            fail("invalid_transaction", "Transaction status must match the native source boundary")
        count = None if expected == "BEGIN" else integer(payload.get("event_count"), minimum=0)
        collections = {}
        if expected == "COMMIT":
            for item in payload.get("data_collections") or []:
                if (
                    not isinstance(item, dict)
                    or not isinstance(item.get("data_collection"), str)
                    or item["data_collection"] in collections
                ):
                    fail("invalid_transaction", "Invalid transaction collection counts")
                collections[item["data_collection"]] = integer(item.get("event_count"), minimum=0)
            if sum(collections.values()) != count:
                fail("invalid_transaction", "Transaction collection counts do not sum to END count")
        boundary = TransactionBoundary(
            "begin" if expected == "BEGIN" else "end",
            integer(offset.get("txId"), minimum=0),
            integer(offset.get("ts_usec")),
            lsn,
            count,
            collections,
        )
    elif kind == "data":
        if (
            not isinstance(payload, dict)
            or payload.get("op") != operation
            or operation not in {"r", "c", "u", "d", "t"}
        ):
            fail()
        schema = value_part.get("schema")
        if not isinstance(schema, dict) or schema.get("type") != "struct":
            fail()
        definitions = {field["field"]: field for field in schema.get("fields", [])}
        if payload.get("before") is not None:
            before = decoder.row(definitions.get("before"), payload["before"])
        if payload.get("after") is not None:
            after = decoder.row(definitions.get("after"), payload["after"])
        if operation in {"r", "c", "u"} and after is None:
            fail("missing_after", "Create/update/snapshot requires a typed after row")
        if operation in {"r", "c", "u"}:
            require_row_key(after, key, identity)
        elif operation == "d" and before is not None:
            require_row_key(before, key, identity)
        source = payload.get("source")
        if (
            not isinstance(source, dict)
            or not isinstance(source.get("schema"), str)
            or not isinstance(source.get("table"), str)
        ):
            fail("missing_resource", "Native PostgreSQL schema and table are required")
        if raw.get("snapshot") != source.get("snapshot"):
            fail("invalid_snapshot", "Snapshot marker does not match native source metadata")
        if source.get("txId") is not None and source["txId"] != offset.get("txId"):
            fail("invalid_transaction", "Source transaction id disagrees with native offset")
        resource_id = source["schema"] + "." + source["table"]
        tx = payload.get("transaction")
        if tx != raw.get("transaction"):
            fail("invalid_transaction", "Transaction metadata does not match envelope")
        if tx is not None:
            member = TransactionMember(
                integer(offset.get("txId"), minimum=0),
                integer(offset.get("ts_usec")),
                integer(tx.get("total_order"), minimum=1),
                integer(tx.get("data_collection_order"), minimum=1),
            )
        elif operation != "r":
            fail("missing_transaction", "Streaming changes require native transaction metadata")
    elif kind == "tombstone":
        if payload is not None:
            fail()
    elif kind not in {"heartbeat", "schema", "control"}:
        fail()
    return DecodedChange(
        raw.get("event_id", ""),
        kind,
        operation,
        key,
        identity,
        before,
        after,
        partition,
        position,
        resource_id,
        raw.get("snapshot"),
        boundary,
        member,
    )


def require_row_key(row, key, identity):
    values = {}
    for field in key:
        value = row.fields.get(field)
        if value is None or value.state != ValueState.VALUE:
            fail("source_row_key_unavailable", "The row must explicitly carry every stable key field")
        values[field] = value.value
    try:
        if canonical_identity_key(values) != identity:
            fail("source_row_key_mismatch", "Native row primary-key values disagree with the event key")
    except ConnectorError:
        fail("unsupported_key_semantics", "Row key values require an explicitly supported stable identity")


def decode_snapshot_notification(raw, decoder):
    key_schema, value_schema = raw["key"]["schema"], raw["value"]["schema"]
    if (
        not isinstance(key_schema, dict)
        or key_schema.get("name") != "io.debezium.connector.common.NotificationKey"
        or any(raw.get(field) is not None for field in ("operation", "transaction", "snapshot"))
    ):
        fail("invalid_snapshot_notification", "Native notification schema or envelope fields do not match")
    key = decoder.row(key_schema, raw["key"]["payload"], check_unavailable=False).complete_values()
    payload = decoder.row(value_schema, raw["value"]["payload"], check_unavailable=False).complete_values()
    if key != {"id": payload.get("id")} or payload.get("aggregate_type") != "Initial Snapshot":
        fail(
            "unsupported_snapshot_notification",
            "Only the pinned native initial snapshot notification is supported",
        )
    try:
        if str(UUID(payload["id"])) != payload["id"]:
            fail()
    except (ValueError, TypeError, KeyError):
        fail("invalid_snapshot_notification", "Native notification identity is malformed")
    integer(payload.get("timestamp"), minimum=0)
    status, extra = payload.get("type"), payload.get("additional_data")
    if status not in {
        "STARTED",
        "IN_PROGRESS",
        "TABLE_SCAN_COMPLETED",
        "COMPLETED",
        "ABORTED",
        "SKIPPED",
    } or not isinstance(extra, dict):
        fail("unsupported_snapshot_notification", "Unknown snapshot notification cannot prove a baseline")
    connector_name = extra.get("connector_name")
    if not isinstance(connector_name, str) or not connector_name:
        fail("invalid_snapshot_notification", "Native connector scope is missing")
    resources = collection = count = scan_status = None
    if status in {"IN_PROGRESS", "TABLE_SCAN_COMPLETED"}:
        resource_text = extra.get("data_collections")
        if not isinstance(resource_text, str):
            fail("missing_snapshot_scope", "Native table notification lacks its actual capture scope")
        resources = tuple(sorted(resource_text.split(",")))
        # The upstream comma-separated identifier format cannot disambiguate
        # identifiers themselves containing commas/dots; do not guess names.
        if (
            not resources
            or len(resources) != len(set(resources))
            or any(len(resource.split(".")) != 2 or not all(resource.split(".")) for resource in resources)
        ):
            fail(
                "unsupported_snapshot_scope",
                "Capture identifiers need an unambiguous schema.table representation",
            )
        collection = extra.get(
            "current_collection_in_progress" if status == "IN_PROGRESS" else "scanned_collection"
        )
        if collection not in resources:
            fail("invalid_snapshot_notification", "Table notification is outside its declared capture scope")
        if status == "TABLE_SCAN_COMPLETED":
            scan_status = extra.get("status")
            raw_count = extra.get("total_rows_scanned")
            if (
                scan_status != "SUCCEEDED"
                or not isinstance(raw_count, str)
                or not re.fullmatch(r"[0-9]{1,19}", raw_count)
            ):
                fail("snapshot_scan_failed", "Every captured table needs a successful exact row count")
            count = int(raw_count)
    return SnapshotNotification(
        status, payload["id"], connector_name, resources, collection, count, scan_status
    )


def validate_transaction_window(
    begin: TransactionBoundary, changes: list[DecodedChange], end: TransactionBoundary
) -> None:
    """Validate one complete ordered window before the caller atomically projects it.

    Caller must preserve stream/generation and deduplicate inbox records first.
    Missing BEGIN/END, holes, reordered rows or mixed source transactions are rejected.
    """
    if (
        begin.phase != "begin"
        or end.phase != "end"
        or (begin.tx_id, begin.source_commit_us) != (end.tx_id, end.source_commit_us)
        or begin.lsn > end.lsn
    ):
        fail("incomplete_transaction", "Native BEGIN/COMMIT boundaries do not match")
    counts = Counter()
    order = 0
    last_lsn = begin.lsn
    expected_partition = changes[0].source_partition if changes else None
    for change in changes:
        position = change.source_position
        if change.source_partition != expected_partition:
            fail("mixed_partition", "Transaction rows cannot cross source partitions")
        if position.get("txId") != begin.tx_id or position.get("ts_usec") != begin.source_commit_us:
            fail("mixed_transaction", "A source transaction cannot contain another transaction's rows")
        if not last_lsn <= position["lsn"] <= end.lsn:
            fail("transaction_order", "Source LSN falls outside its ordered transaction window")
        last_lsn = position["lsn"]
        if change.kind == "tombstone":
            continue  # deletion marker is not another source row for END event_count
        member = change.member
        if change.kind != "data" or member is None or change.resource_id is None:
            fail("incomplete_transaction", "Unexpected non-row inside a transaction")
        order += 1
        counts[change.resource_id] += 1
        if member.total_order != order or member.data_collection_order != counts[change.resource_id]:
            fail("transaction_order", "Transaction row ordinals contain a gap, duplicate or reorder")
    if order != end.event_count or dict(counts) != end.data_collections:
        fail("incomplete_transaction", "Transaction rows do not match native END counts")
