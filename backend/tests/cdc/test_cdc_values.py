"""Codec and merge checks use captured native rows plus explicit corruptions."""

import copy
import hashlib
import json
import math
from dataclasses import replace
from datetime import UTC, date, datetime, time, timedelta, timezone
from decimal import Decimal, localcontext
from uuid import UUID

import pytest
from ontology.cdc.decode import DecodedRow, ExactTemporal, FieldValue, ValueState, decode_pg_event
from ontology.cdc.values import (
    CODEC,
    CdcValueError,
    RowBinding,
    ValueBudget,
    decode_row,
    encode_row,
    mapping_values,
    merge_row,
)
from ontology.cdc.windows import schema_fingerprint

from tests.support.paths import REPO_ROOT

PROTOCOL = REPO_ROOT / "integrations/debezium/protocol"


def row(**values):
    return DecodedRow(
        {
            name: value
            if type(value) is FieldValue
            else FieldValue(ValueState.NULL if value is None else ValueState.VALUE, value)
            for name, value in values.items()
        }
    )


def persisted(value, **kwargs):
    return decode_row(json.loads(json.dumps(encode_row(value, **kwargs))), **kwargs)


def binding(event):
    decoded = decode_pg_event(event)
    return RowBinding(
        decoded.resource_id,
        schema_fingerprint(event),
        decoded.identity_key,
        json.loads(event["identity_material"])["generation"],
    )


def simple_binding():
    return RowBinding("public.items", "a" * 64, {"id": {"type": "integer", "value": "1"}}, "test-generation")


def test_all_types_states_order_and_business_tags_roundtrip_without_collision():
    named_offset = timezone(timedelta(hours=8), "source offset")
    original = row(
        missing=FieldValue(ValueState.MISSING),
        unavailable=FieldValue(ValueState.UNAVAILABLE),
        null=None,
        integer=9223372036854775807,
        boolean=True,
        binary=b"\x00\xff",
        uuid=UUID(int=123),
        decimal=Decimal("-0.000"),
        precise=Decimal("12345678901234567890.12345678901234567890"),
        day=date(1969, 12, 31),
        naive=datetime(2026, 10, 28, 12, 30, 59, 123456, fold=1),
        instant=datetime(2026, 10, 28, 12, 30, 59, 123456, tzinfo=named_offset, fold=1),
        clock=time(1, 2, 3, 999999, tzinfo=UTC, fold=1),
        signed_float=-0.0,
        nanos=ExactTemporal("io.debezium.time.NanoTimestamp", 123456789, "ns"),
        zoned_nanos=ExactTemporal(
            "io.debezium.time.ZonedTimestamp", "2026-01-01T08:00:00.123456789+08:00", "iso8601"
        ),
        business={"$type": "decimal", "tag": "bytes", "field": ["int", "00001"], "z": 1, "a": None},
        nested={"scalar": FieldValue(ValueState.VALUE, Decimal("1.00")), "null": FieldValue(ValueState.NULL)},
        map_pairs=[([1, 2], {"nested": FieldValue(ValueState.VALUE, "值")})],
    )
    with localcontext() as context:
        context.prec = 2
        restored = persisted(original)
    assert restored == original
    assert restored.fields["decimal"].value.as_tuple() == original.fields["decimal"].value.as_tuple()
    assert restored.fields["precise"].value.as_tuple() == original.fields["precise"].value.as_tuple()
    assert list(restored.fields) == list(original.fields)
    assert list(restored.fields["business"].value) == ["$type", "tag", "field", "z", "a"]
    assert type(restored.fields["map_pairs"].value[0]) is tuple
    assert restored.fields["instant"].value.tzname() == "source offset"
    assert restored.fields["instant"].value.fold == restored.fields["naive"].value.fold == 1
    assert math.copysign(1, restored.fields["signed_float"].value) == -1
    assert encode_row(restored) == encode_row(original)


def test_real_captured_rows_and_two_same_key_updates_keep_exact_values_and_order():
    events = json.loads((PROTOCOL / "pg-events.json").read_text(encoding="utf-8"))["events"]
    changes = [(event, decode_pg_event(event)) for event in events]
    first_event, first = next(
        (event, change) for event, change in changes if change.operation == "r" and change.key == {"id": 1}
    )
    fixed = binding(first_event)
    state = merge_row(persisted(first.after), operation="r", binding=fixed)
    updates = [
        (event, change) for event, change in changes if change.operation == "u" and change.key == {"id": 1}
    ]
    assert len(updates) == 2
    assert updates[0][1].member.total_order < updates[1][1].member.total_order
    for event, change in updates:
        current = binding(event)
        state = merge_row(
            change.after, operation="u", binding=current, previous=persisted(state), previous_binding=fixed
        )
        fixed = current
    result = mapping_values(state, fields=["id", "name", "amount", "optional"])
    assert result["id"] == 1 and type(result["id"]) is int
    assert result["name"] == "更新一次"
    assert result["amount"] == "123456789012.123456789012345678"
    assert result["optional"] is None
    assert state.fields["raw"].value == b"\x00\xff"
    with pytest.raises(CdcValueError) as error:
        mapping_values(state)
    assert error.value.code == "unsupported_mapping_type"


def test_merge_reconstructs_update_and_explicit_null_clears_without_mutating_input():
    fixed = simple_binding()
    old = persisted(row(id=1, body="previous complete body", tags={"x": [1, 2]}))
    incoming = row(id=1, body=FieldValue(ValueState.UNAVAILABLE), tags={"x": [3]})
    merged = merge_row(incoming, operation="u", binding=fixed, previous=old, previous_binding=fixed)
    assert mapping_values(merged)["body"] == "previous complete body"
    merged.fields["tags"].value["x"].append(4)
    assert old.fields["tags"].value == {"x": [1, 2]}
    assert incoming.fields["tags"].value == {"x": [3]}
    cleared = merge_row(
        row(id=1, body=None, tags={"x": [5]}),
        operation="u",
        binding=fixed,
        previous=persisted(merged),
        previous_binding=fixed,
    )
    assert cleared.fields["body"].state == ValueState.NULL
    assert mapping_values(cleared)["body"] is None


def test_actual_pgoutput_toast_fixture_restores_persisted_value_and_clears_null():
    captured = json.loads((PROTOCOL / "pg-toast-events.json").read_text(encoding="utf-8"))
    assert captured["toast_relation_bytes"] > 0
    assert captured["replica_identity"] == "DEFAULT"
    events = [event for event in captured["events"] if event["operation"] == "u"]
    assert len(events) == 3
    full, unchanged, cleared = [decode_pg_event(event) for event in events]
    assert full.after.fields["optional"].state == ValueState.VALUE
    body = full.after.fields["optional"].value
    assert len(body) == captured["text_length"]
    assert hashlib.sha256(body.encode()).hexdigest() == captured["text_sha256"]
    assert unchanged.after.fields["optional"].state == ValueState.UNAVAILABLE
    assert cleared.after.fields["optional"].state == ValueState.NULL
    assert unchanged.before is None  # DEFAULT does not provide a complete old row.
    original = copy.deepcopy(captured)
    restored = merge_row(
        persisted(unchanged.after),
        operation="u",
        binding=binding(events[1]),
        previous=persisted(full.after),
        previous_binding=binding(events[0]),
    )
    assert restored.fields["optional"].value == body
    assert unchanged.after.fields["optional"].state == ValueState.UNAVAILABLE
    cleared_row = merge_row(
        persisted(cleared.after),
        operation="u",
        binding=binding(events[2]),
        previous=persisted(restored),
        previous_binding=binding(events[1]),
    )
    assert mapping_values(cleared_row, fields=["optional"]) == {"optional": None}
    with pytest.raises(CdcValueError) as error:
        merge_row(unchanged.after, operation="u", binding=binding(events[1]))
    assert error.value.code == "unavailable_without_previous"
    assert captured == original


def test_bindings_reject_ambiguous_json_metadata_before_merging():
    fixed = simple_binding()
    for identity in (
        {1: {"type": "integer", "value": "1"}},
        {"id": {"type": "integer", "value": 1}},
        {"id": ["integer", "1"]},
        {"id": {"type": "integer", "value": "1", "extra": True}},
    ):
        with pytest.raises(CdcValueError) as error:
            merge_row(row(id=1), operation="r", binding=replace(fixed, identity_key=identity))
        assert error.value.code == "missing_row_binding"
    with pytest.raises(CdcValueError):
        merge_row(row(id=1), operation="r", binding=replace(fixed, resource_id="\ud800"))


@pytest.mark.parametrize("change", ["resource_id", "schema_fingerprint", "identity_key", "source_generation"])
def test_merge_requires_same_explicit_binding(change):
    fixed = simple_binding()
    wrong = replace(
        fixed,
        **{
            change: {"id": {"type": "integer", "value": "2"}}
            if change == "identity_key"
            else "b" * 64
            if change == "schema_fingerprint"
            else "different"
        },
    )
    with pytest.raises(CdcValueError) as error:
        merge_row(
            row(id=1, body=FieldValue(ValueState.UNAVAILABLE)),
            operation="u",
            binding=fixed,
            previous=row(id=1, body="known"),
            previous_binding=wrong,
        )
    assert error.value.code == "incompatible_previous_row"


@pytest.mark.parametrize("operation", ["r", "c", "d", "t"])
def test_non_update_cannot_recover_unavailable(operation):
    fixed = simple_binding()
    with pytest.raises(CdcValueError):
        merge_row(
            row(body=FieldValue(ValueState.UNAVAILABLE)),
            operation=operation,
            binding=fixed,
            previous=row(body="known"),
            previous_binding=fixed,
        )


def test_missing_and_incomplete_previous_never_become_null_or_current_values():
    fixed = simple_binding()
    with pytest.raises(CdcValueError) as error:
        merge_row(
            row(body=FieldValue(ValueState.MISSING)),
            operation="u",
            binding=fixed,
            previous=row(body="known"),
            previous_binding=fixed,
        )
    assert error.value.code == "missing_source_field"
    for previous in (
        None,
        row(body=None),
        row(body=FieldValue(ValueState.MISSING)),
        row(body={"nested": FieldValue(ValueState.UNAVAILABLE)}),
    ):
        with pytest.raises(CdcValueError):
            merge_row(
                row(body=FieldValue(ValueState.UNAVAILABLE)),
                operation="u",
                binding=fixed,
                previous=previous,
                previous_binding=fixed if previous else None,
            )
    with pytest.raises(CdcValueError) as error:
        merge_row(
            row(body="current", added="new"),
            operation="u",
            binding=fixed,
            previous=row(body="known"),
            previous_binding=fixed,
        )
    assert error.value.code == "incompatible_previous_row"
    with pytest.raises(CdcValueError):
        merge_row(
            row(body={"nested": FieldValue(ValueState.UNAVAILABLE)}),
            operation="u",
            binding=fixed,
            previous=row(body={"nested": FieldValue(ValueState.VALUE, "known")}),
            previous_binding=fixed,
        )


def test_mapping_conversions_are_explicit_and_only_selected_fields_are_converted():
    source = row(
        number=9223372036854775807,
        decimal=Decimal("1.00000000000000000001"),
        uid=UUID(int=123),
        day=date(2026, 10, 28),
        instant=datetime(2026, 10, 28, 12, 0, tzinfo=UTC),
        blob=b"\x00\xff",
        clock=time(12),
        nano=ExactTemporal("io.debezium.time.NanoTime", 1, "ns"),
        missing=FieldValue(ValueState.MISSING),
        unavailable=FieldValue(ValueState.UNAVAILABLE),
    )
    result = mapping_values(source, fields=(key for key in ("number", "decimal", "uid", "day", "instant")))
    assert result == {
        "number": 9223372036854775807,
        "decimal": "1.00000000000000000001",
        "uid": "00000000-0000-0000-0000-00000000007b",
        "day": "2026-10-28",
        "instant": "2026-10-28T12:00:00+00:00",
    }
    for field in ("blob", "clock", "nano", "missing", "unavailable"):
        with pytest.raises(CdcValueError):
            mapping_values(source, fields=[field])
    for selection in ("number", ["number", "number"], ["absent"]):
        with pytest.raises(CdcValueError):
            mapping_values(source, fields=selection)
    assert mapping_values(source, fields=[]) == {}


@pytest.mark.parametrize(
    "invalid",
    [
        ["unknown", None],
        ["int", "01"],
        ["int", "-0"],
        ["int", True],
        ["float", "nan"],
        ["float", "0x1.0p+999999"],
        ["bytes", "!!!!"],
        ["bytes", "AP9="],
        ["decimal", [0, "001", "0"]],
        ["decimal", [False, "1", "0"]],
        ["field", ["null", ["str", "not null"]]],
        ["field", ["value", ["null", None]]],
        ["map", [[["bool", True], ["int", "1"]], [["int", "1"], ["int", "2"]]]],
    ],
)
def test_corrupt_codec_tags_payloads_and_equal_map_keys_fail_closed(invalid):
    encoded = [CODEC, [["x", invalid if invalid[0] == "field" else ["field", ["value", invalid]]]]]
    with pytest.raises(CdcValueError):
        decode_row(encoded)


def test_duplicate_fields_unknown_versions_nonfinite_values_and_invalid_states_rejected():
    encoded = encode_row(row(x=1))
    with pytest.raises(CdcValueError):
        decode_row(["cdc-row-v2", encoded[1]])
    with pytest.raises(CdcValueError):
        decode_row([CODEC, encoded[1] + encoded[1]])
    for value in (
        float("inf"),
        Decimal("NaN"),
        object(),
        "\ud800",
        FieldValue(ValueState.VALUE, None),
        FieldValue(ValueState.NULL, "unexpected"),
    ):
        with pytest.raises(CdcValueError):
            encode_row(row(x=value))


def test_budgets_cover_encode_decode_depth_scalars_escape_expansion_and_merge_result():
    for value, budget in (
        (row(x="中文" * 40), ValueBudget(max_bytes=100)),
        (row(x='"' * 60), ValueBudget(max_bytes=100)),
        (row(x=10**1000), ValueBudget()),
        (row(x=Decimal("1E+100001")), ValueBudget()),
        (row(x=Decimal(12345)), ValueBudget(max_decimal_digits=4)),
        (row(x=[1, 2, 3]), ValueBudget(max_values=3)),
        (row(x=[[[[1]]]]), ValueBudget(max_depth=3)),
    ):
        with pytest.raises(CdcValueError) as error:
            encode_row(value, budget=budget)
        assert error.value.code == "value_budget"
    ordinary = encode_row(row(x="a" * 200))
    with pytest.raises(CdcValueError) as error:
        decode_row(ordinary, budget=ValueBudget(max_bytes=100))
    assert error.value.code == "value_budget"
    cyclic = []
    cyclic.append(cyclic)
    with pytest.raises(CdcValueError) as error:
        encode_row(row(x=cyclic))
    assert error.value.code == "value_budget"
    fixed = simple_binding()
    before = row(a="a" * 350, b="")
    after = row(a=FieldValue(ValueState.UNAVAILABLE), b="b" * 350)
    budget = ValueBudget(max_bytes=650)
    encode_row(before, budget=budget)
    encode_row(after, budget=budget)
    with pytest.raises(CdcValueError) as error:
        merge_row(after, operation="u", binding=fixed, previous=before, previous_binding=fixed, budget=budget)
    assert error.value.code == "value_budget"


def test_read_fixture_does_not_mutate_raw_native_event():
    events = json.loads((PROTOCOL / "pg-events.json").read_text(encoding="utf-8"))["events"]
    raw = next(event for event in events if event["operation"] == "r")
    saved = copy.deepcopy(raw)
    decoded = decode_pg_event(raw)
    persisted(decoded.after)
    assert raw == saved
