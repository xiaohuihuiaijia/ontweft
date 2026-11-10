import base64
import copy
import json
from dataclasses import replace
from datetime import UTC, date, datetime, time
from decimal import Decimal, localcontext
from uuid import UUID

import pytest
from ontology.cdc.decode import (
    CdcDecodeError,
    ConnectDecoder,
    ExactTemporal,
    ValueState,
    decode_pg_event,
    validate_transaction_window,
)
from ontology.connectors.base import canonical_identity_key

from tests.support.paths import REPO_ROOT


@pytest.fixture
def native_events():
    path = REPO_ROOT / "integrations/debezium/protocol/pg-events.json"
    return json.loads(path.read_text(encoding="utf-8"))["events"]


def encoded_integer(value):
    return base64.b64encode(value.to_bytes((value.bit_length() + 8) // 8, "big", signed=True)).decode()


def test_real_captured_pg_events_decode_and_validate_changing_transaction_display_ids(native_events):
    changes = [decode_pg_event(event) for event in native_events]
    snapshot = changes[0]
    assert snapshot.after.fields["amount"].value == Decimal("123456789012.123456789012345678")
    assert snapshot.after.fields["raw"].value == b"\x00\xff"
    assert snapshot.after.fields["optional"].state == ValueState.NULL
    assert snapshot.identity_key == canonical_identity_key({"id": 1})
    assert snapshot.snapshot == "first"
    begin = next(
        change.boundary for change in changes if change.boundary and change.boundary.phase == "begin"
    )
    end = next(change.boundary for change in changes if change.boundary and change.boundary.phase == "end")
    members = [change for change in changes if change.member and change.member.tx_id == begin.tx_id]
    validate_transaction_window(begin, members, end)
    display_ids = {
        event["transaction"]["id"]
        for event in native_events
        if event["source_offset"].get("txId") == begin.tx_id
    }
    assert len(display_ids) > 1  # observed upstream behavior, not a mocked id assumption
    assert next(change for change in changes if change.kind == "tombstone").after is None
    assert next(change for change in changes if change.operation == "t").key is None
    with pytest.raises(CdcDecodeError, match="ordinals"):
        validate_transaction_window(begin, members[::-1], end)
    with pytest.raises(CdcDecodeError, match="END counts"):
        validate_transaction_window(begin, members[:-1], end)
    with pytest.raises(CdcDecodeError, match="partitions"):
        validate_transaction_window(
            begin, [members[0], replace(members[1], source_partition={"server": "other"}), *members[2:]], end
        )


def test_decimal_variable_scale_and_typed_keys_ignore_decimal_context_precision():
    decoder = ConnectDecoder()
    coefficient = 123456789012123456789012345678
    with localcontext() as context:
        context.prec = 3
        fixed = decoder.decode(
            {"type": "bytes", "name": "org.apache.kafka.connect.data.Decimal", "parameters": {"scale": "18"}},
            encoded_integer(coefficient),
        )
        variable = decoder.decode(
            {"type": "struct", "name": "io.debezium.data.VariableScaleDecimal"},
            {"scale": 18, "value": encoded_integer(coefficient)},
        )
        negative = decoder.decode(
            {"type": "struct", "name": "io.debezium.data.VariableScaleDecimal"},
            {"scale": -2, "value": encoded_integer(-7)},
        )
    assert fixed == variable == Decimal("123456789012.123456789012345678")
    assert negative == Decimal(-700)
    assert canonical_identity_key({"id": fixed}) == canonical_identity_key(
        {"id": Decimal("123456789012.1234567890123456780")}
    )


def test_dates_local_instants_uuid_bytes_and_nanoseconds_are_lossless():
    decode = ConnectDecoder().decode
    assert decode({"type": "int32", "name": "io.debezium.time.Date"}, -1) == date(1969, 12, 31)
    assert decode({"type": "int64", "name": "io.debezium.time.MicroTimestamp"}, -1) == datetime(
        1969, 12, 31, 23, 59, 59, 999999
    )
    assert decode({"type": "int64", "name": "org.apache.kafka.connect.data.Timestamp"}, 1) == datetime(
        1970, 1, 1, 0, 0, 0, 1000, tzinfo=UTC
    )
    assert decode({"type": "int64", "name": "io.debezium.time.MicroTime"}, 1) == time(0, 0, 0, 1)
    precise = decode({"type": "int64", "name": "io.debezium.time.NanoTimestamp"}, 123456789)
    assert precise == ExactTemporal("io.debezium.time.NanoTimestamp", 123456789, "ns")
    zoned = decode(
        {"type": "string", "name": "io.debezium.time.ZonedTimestamp"}, "2026-01-01T08:00:00.123456789+08:00"
    )
    assert isinstance(zoned, ExactTemporal)
    instant = decode(
        {"type": "string", "name": "io.debezium.time.ZonedTimestamp"}, "2026-01-01T08:00:00.123456+08:00"
    )
    assert canonical_identity_key({"id": instant}) == canonical_identity_key(
        {"id": datetime(2026, 1, 1, 0, 0, 0, 123456, tzinfo=UTC)}
    )
    value = UUID("00000000-0000-0000-0000-000000000123")
    assert decode({"type": "string", "name": "io.debezium.data.Uuid"}, str(value)) == value
    assert decode({"type": "bytes"}, "AP8=") == b"\x00\xff"
    assert decode({"type": "float"}, 0.1) == 0.10000000149011612  # float32 source identity, not float64 0.1


def test_missing_null_unavailable_and_literal_placeholder_are_never_silently_filled():
    schema = {
        "type": "struct",
        "fields": [
            {"field": field, "type": "string", "optional": True, "default": "unsafe-default"}
            for field in ("missing", "null", "toast", "value")
        ],
    }
    row = ConnectDecoder().row(schema, {"null": None, "toast": "__debezium_unavailable_value", "value": ""})
    assert {key: value.state for key, value in row.fields.items()} == {
        "missing": ValueState.MISSING,
        "null": ValueState.NULL,
        "toast": ValueState.UNAVAILABLE,
        "value": ValueState.VALUE,
    }
    with pytest.raises(CdcDecodeError, match="merge policy"):
        row.complete_values()
    # A source where this is known to be literal business text must explicitly disable/replace the marker policy.
    literal = ConnectDecoder(unavailable_value_placeholder=None).row(
        schema, {"toast": "__debezium_unavailable_value"}
    )
    assert literal.fields["toast"].state == ValueState.VALUE
    bytes_row = ConnectDecoder().row(
        {"type": "struct", "fields": [{"field": "toast", "type": "bytes"}]},
        {"toast": base64.b64encode(b"__debezium_unavailable_value").decode()},
    )
    assert bytes_row.fields["toast"].state == ValueState.UNAVAILABLE


@pytest.mark.parametrize(
    ("schema", "payload"),
    [
        ({"type": "int64"}, True),
        ({"type": "int8"}, 128),
        ({"type": "bytes"}, "not-base64"),
        ({"type": "string", "name": "io.debezium.time.Unknown"}, "x"),
        ({"type": "string"}, 1),
        ({"type": "float64"}, float("nan")),
        (None, "value"),
        (
            {"type": "string", "name": "org.apache.kafka.connect.data.Decimal", "parameters": {"scale": "2"}},
            "AA==",
        ),
    ],
)
def test_schema_mismatch_and_unknown_logical_types_fail_closed(schema, payload):
    with pytest.raises(CdcDecodeError):
        ConnectDecoder().decode(schema, payload)


def test_transaction_native_boundary_and_streaming_metadata_are_required(native_events):
    begin = copy.deepcopy(next(event for event in native_events if event["event_kind"] == "transaction"))
    begin["source_offset"]["messageType"] = "UPDATE"
    with pytest.raises(CdcDecodeError, match="native source boundary"):
        decode_pg_event(begin)
    update = copy.deepcopy(next(event for event in native_events if event["operation"] == "u"))
    update["transaction"] = update["value"]["payload"]["transaction"] = None
    with pytest.raises(CdcDecodeError, match="transaction metadata"):
        decode_pg_event(update)


def test_key_and_payload_schema_drift_and_budget_are_rejected(native_events):
    event = copy.deepcopy(native_events[0])
    event["key"]["payload"]["extra"] = "not-in-schema"
    with pytest.raises(CdcDecodeError, match="absent from"):
        decode_pg_event(event)
    with pytest.raises(CdcDecodeError, match="budget"):
        ConnectDecoder(max_values=1).decode({"type": "array", "items": {"type": "string"}}, ["a", "b"])


@pytest.mark.parametrize("event_kind", ["data", "tombstone"])
def test_empty_struct_key_cannot_be_a_stable_row_identity(native_events, event_kind):
    event = copy.deepcopy(next(item for item in native_events if item["event_kind"] == event_kind))
    event["key"] = {"schema": {"type": "struct", "fields": []}, "payload": {}}
    with pytest.raises(CdcDecodeError) as error:
        decode_pg_event(event)
    assert error.value.code == "missing_source_key"


@pytest.mark.parametrize("operation", ["r", "u", "c"])
def test_native_event_key_and_after_row_must_agree(native_events, operation):
    source_operation = "r" if operation == "c" else operation
    event = copy.deepcopy(next(item for item in native_events if item["operation"] == source_operation))
    # The mismatch is checked before the independent transaction metadata contract.
    event["operation"] = event["value"]["payload"]["op"] = operation
    event["value"]["payload"]["after"]["id"] = event["key"]["payload"]["id"] + 1
    with pytest.raises(CdcDecodeError) as error:
        decode_pg_event(event)
    assert error.value.code == "source_row_key_mismatch"


def test_native_row_missing_key_cannot_be_replaced_by_envelope_key(native_events):
    event = copy.deepcopy(native_events[0])
    del event["value"]["payload"]["after"]["id"]
    with pytest.raises(CdcDecodeError) as error:
        decode_pg_event(event)
    assert error.value.code == "source_row_key_unavailable"
