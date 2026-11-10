"""Lossless decoded-row storage and explicit, offline TOAST reconciliation.

Persist ``encode_row(decoded)`` as JSON and restore with ``decode_row(payload)``.
Every value is [tag, payload]; business dictionaries are ordered key/value lists,
so a business key such as "$type" or "field" can never impersonate a codec tag.
Raw CdcEvent remains authoritative. This module neither establishes a baseline
nor proves permissions/schema compatibility or persists data by itself.

Example: merge_row(after, operation="u", binding=current_binding,
                  previous=decode_row(saved_payload), previous_binding=saved_binding)
Mapping callers must select their referenced source fields explicitly when they
do not map the whole row: mapping_values(merged, fields=["id", "amount"]).
"""

from __future__ import annotations

import base64
import binascii
import json
import math
import re
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, datetime, time, timezone
from decimal import Decimal
from typing import Any
from uuid import UUID

from ontology.cdc.decode import TEMPORAL_TYPES, DecodedRow, ExactTemporal, FieldValue, ValueState

CODEC = "cdc-row-v1"


class CdcValueError(ValueError):
    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(message)


def fail(code="invalid_cdc_value", message="Invalid or unsupported typed CDC value"):
    raise CdcValueError(code, message)


@dataclass(frozen=True)
class ValueBudget:
    max_depth: int = 64
    max_values: int = 100_000
    max_bytes: int = 8 * 1024 * 1024
    max_integer_digits: int = 1000
    max_decimal_digits: int = 100_000

    def __post_init__(self):
        for value in vars(self).values():
            if type(value) is not int or value <= 0:
                fail("value_budget", "Budgets must be positive integers")
        if self.max_depth > 64 or self.max_integer_digits > 1000:
            fail("value_budget", "Depth and integer limits exceed the supported codec bounds")


DEFAULT_BUDGET = ValueBudget()


class _Budget:
    def __init__(self, limits):
        self.limits, self.values, self.bytes = limits, 0, 0

    def visit(self, depth):
        self.values += 1
        if depth > self.limits.max_depth or self.values > self.limits.max_values:
            fail("value_budget", "Typed CDC depth or value budget exceeded")

    def string(self, value):
        if type(value) is not str:
            fail()
        if len(value) > self.limits.max_bytes:
            fail("value_budget", "Typed CDC string exceeds byte budget")
        try:
            self.bytes += len(value.encode("utf-8"))
        except UnicodeError:
            fail("invalid_cdc_value", "Strings must contain valid Unicode scalar values")
        if self.bytes > self.limits.max_bytes:
            fail("value_budget", "Typed CDC byte budget exceeded")
        return value


def _json_size(payload, budget):
    # A final serialized-size check includes escaping and container overhead.
    # Traversal budgets have already bounded allocation and recursion.
    try:
        size = len(
            json.dumps(payload, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")
        )
    except (ValueError, TypeError, UnicodeError, RecursionError):
        fail()
    if size > budget.max_bytes:
        fail("value_budget", "Serialized CDC value exceeds byte budget")


def _integer(value, budget):
    if type(value) is not int:
        fail()
    if value.bit_length() > budget.limits.max_integer_digits * 4:
        fail("value_budget", "Integer exceeds digit budget")
    text = str(value)
    if len(text.lstrip("-")) > budget.limits.max_integer_digits:
        fail("value_budget", "Integer exceeds digit budget")
    return budget.string(text)


def _read_integer(payload, budget):
    text = budget.string(payload)
    if len(text.lstrip("-")) > budget.limits.max_integer_digits:
        fail("value_budget", "Integer exceeds digit budget")
    if re.fullmatch(r"0|-?[1-9][0-9]*", text) is None:
        fail()
    return int(text)


def _temporal_payload(value):
    if value.tzinfo is not None and type(value.tzinfo) is not timezone:
        fail("unsupported_timezone", "Only decoded fixed-offset or naive temporal values are supported")
    return [value.isoformat(), value.fold, value.tzname() if value.tzinfo else None]


def _exact(value):
    if type(value.logical_type) is not str or type(value.unit) is not str:
        fail()
    expected = (
        "iso8601"
        if "Zoned" in value.logical_type
        else "days"
        if value.logical_type.endswith(".Date")
        else ("ns" if "Nano" in value.logical_type else "us" if "Micro" in value.logical_type else "ms")
    )
    if value.logical_type not in TEMPORAL_TYPES or value.unit != expected:
        fail()
    if type(value.raw_value) is not (str if expected == "iso8601" else int):
        fail()


def _encode(value, budget, depth):
    budget.visit(depth)
    if value is None:
        return ["null", None]
    if type(value) is bool:
        return ["bool", value]
    if type(value) is int:
        return ["int", _integer(value, budget)]
    if type(value) is float:
        if not math.isfinite(value):
            fail()
        return ["float", budget.string(value.hex())]
    if type(value) is str:
        return ["str", budget.string(value)]
    if type(value) is bytes:
        if len(value) > budget.limits.max_bytes * 3 // 4:
            fail("value_budget", "Binary value exceeds byte budget")
        return ["bytes", budget.string(base64.b64encode(value).decode("ascii"))]
    if type(value) is Decimal:
        if not value.is_finite():
            fail()
        parts = value.as_tuple()
        if len(parts.digits) > budget.limits.max_decimal_digits or abs(parts.exponent) > 100_000:
            fail("value_budget", "Decimal coefficient or exponent exceeds budget")
        return [
            "decimal",
            [
                parts.sign,
                budget.string("".join(str(x) for x in parts.digits)),
                _integer(parts.exponent, budget),
            ],
        ]
    if type(value) is UUID:
        return ["uuid", budget.string(str(value))]
    if type(value) is datetime:
        payload = _temporal_payload(value)
        budget.string(payload[0])
        if payload[2] is not None:
            budget.string(payload[2])
        return ["datetime", payload]
    if type(value) is time:
        payload = _temporal_payload(value)
        budget.string(payload[0])
        if payload[2] is not None:
            budget.string(payload[2])
        return ["time", payload]
    if type(value) is date:
        return ["date", budget.string(value.isoformat())]
    if type(value) is ExactTemporal:
        _exact(value)
        return [
            "exact_temporal",
            [
                budget.string(value.logical_type),
                _encode(value.raw_value, budget, depth + 1),
                budget.string(value.unit),
            ],
        ]
    if type(value) is FieldValue:
        if not isinstance(value.state, ValueState) or (
            value.state != ValueState.VALUE and value.value is not None
        ):
            fail()
        if value.state == ValueState.VALUE and value.value is None:
            fail("invalid_field_state", "A null field must use the explicit NULL state")
        return ["field", [value.state.value, _encode(value.value, budget, depth + 1)]]
    if type(value) is dict:
        return [
            "map",
            [
                [_encode(key, budget, depth + 1), _encode(item, budget, depth + 1)]
                for key, item in value.items()
            ],
        ]
    if type(value) in {list, tuple}:
        return [
            "list" if type(value) is list else "tuple",
            [_encode(item, budget, depth + 1) for item in value],
        ]
    fail("unsupported_value_type", "Value needs an explicit typed CDC codec")


def _array(value, length=None):
    if type(value) is not list or length is not None and len(value) != length:
        fail()
    return value


def _decode(encoded, budget, depth):
    budget.visit(depth)
    tag, payload = _array(encoded, 2)
    if type(tag) is not str:
        fail()
    if tag == "null":
        if payload is not None:
            fail()
        return None
    if tag == "bool":
        if type(payload) is not bool:
            fail()
        return payload
    if tag == "int":
        return _read_integer(payload, budget)
    if tag == "float":
        text = budget.string(payload)
        try:
            result = float.fromhex(text)
        except (ValueError, OverflowError):
            fail()
        if not math.isfinite(result) or result.hex() != text:
            fail()
        return result
    if tag == "str":
        return budget.string(payload)
    if tag == "bytes":
        text = budget.string(payload)
        try:
            result = base64.b64decode(text, validate=True)
        except (ValueError, binascii.Error):
            fail()
        if base64.b64encode(result).decode("ascii") != text:
            fail()
        return result
    if tag == "decimal":
        sign, digits, exponent = _array(payload, 3)
        digits = budget.string(digits)
        exponent = _read_integer(exponent, budget)
        if len(digits) > budget.limits.max_decimal_digits or abs(exponent) > 100_000:
            fail("value_budget", "Decimal coefficient or exponent exceeds budget")
        if type(sign) is not int or sign not in (0, 1) or re.fullmatch(r"0|[1-9][0-9]*", digits) is None:
            fail()
        return Decimal((sign, tuple(int(char) for char in digits), exponent))
    if tag == "uuid":
        text = budget.string(payload)
        try:
            result = UUID(text)
        except ValueError:
            fail()
        if str(result) != text:
            fail()
        return result
    if tag == "date":
        text = budget.string(payload)
        try:
            result = date.fromisoformat(text)
        except ValueError:
            fail()
        if result.isoformat() != text:
            fail()
        return result
    if tag in {"datetime", "time"}:
        text, fold, name = _array(payload, 3)
        budget.string(text)
        if type(fold) is not int or fold not in (0, 1):
            fail()
        try:
            result = (datetime if tag == "datetime" else time).fromisoformat(text).replace(fold=fold)
        except ValueError:
            fail()
        if result.isoformat() != text or (result.tzinfo is None) != (name is None):
            fail()
        if name is not None:
            result = result.replace(tzinfo=timezone(result.utcoffset(), budget.string(name)))
        return result
    if tag == "exact_temporal":
        name, raw, unit = _array(payload, 3)
        result = ExactTemporal(budget.string(name), _decode(raw, budget, depth + 1), budget.string(unit))
        _exact(result)
        return result
    if tag == "field":
        state, raw = _array(payload, 2)
        if type(state) is not str:
            fail()
        try:
            state = ValueState(state)
        except ValueError:
            fail()
        value = _decode(raw, budget, depth + 1)
        if (state == ValueState.VALUE) == (value is None):
            fail("invalid_field_state", "State and field payload disagree")
        return FieldValue(state, value)
    if tag in {"list", "tuple"}:
        items = [_decode(item, budget, depth + 1) for item in _array(payload)]
        return items if tag == "list" else tuple(items)
    if tag == "map":
        result = {}
        for pair in _array(payload):
            key, value = _array(pair, 2)
            key = _decode(key, budget, depth + 1)
            try:
                duplicate = key in result
            except TypeError:
                fail("invalid_map_key", "Dictionary keys must be hashable; typed map-pair lists remain lists")
            if duplicate:
                fail("duplicate_map_key", "Map entries cannot collapse into equal Python keys")
            result[key] = _decode(value, budget, depth + 1)
        return result
    fail("unknown_value_tag", "Unknown typed CDC value tag")


def encode_row(row: DecodedRow, *, budget: ValueBudget = DEFAULT_BUDGET) -> list:
    """Encode all states and types; incomplete rows may be retained as evidence."""
    if type(row) is not DecodedRow or type(row.fields) is not dict:
        fail()
    counter = _Budget(budget)
    entries = []
    for name, value in row.fields.items():
        counter.string(name)
        if type(value) is not FieldValue:
            fail()
        entries.append([name, _encode(value, counter, 0)])
    result = [CODEC, entries]
    _json_size(result, budget)
    return result


def decode_row(encoded: list, *, budget: ValueBudget = DEFAULT_BUDGET) -> DecodedRow:
    """Restore a stored codec document; reject unknown versions and ambiguous keys."""
    codec, entries = _array(encoded, 2)
    if codec != CODEC:
        fail("unknown_row_codec", "Unsupported decoded row codec version")
    counter, fields = _Budget(budget), {}
    for entry in _array(entries):
        name, value = _array(entry, 2)
        counter.string(name)
        if name in fields:
            fail("duplicate_field", "Decoded row has duplicate field names")
        result = _decode(value, counter, 0)
        if type(result) is not FieldValue:
            fail()
        fields[name] = result
    _json_size(encoded, budget)
    return DecodedRow(fields)


@dataclass(frozen=True)
class RowBinding:
    resource_id: str
    schema_fingerprint: str
    identity_key: dict[str, Any]
    source_generation: str


def _binding(binding, budget):
    if (
        type(binding) is not RowBinding
        or type(binding.resource_id) is not str
        or type(binding.source_generation) is not str
        or not binding.resource_id
        or not binding.source_generation
    ):
        fail("missing_row_binding", "A frozen resource, generation, schema and typed identity are required")
    if (
        type(binding.schema_fingerprint) is not str
        or re.fullmatch(r"[a-f0-9]{64}", binding.schema_fingerprint) is None
    ):
        fail("missing_row_binding", "A fixed schema fingerprint is required")
    if type(binding.identity_key) is not dict or not binding.identity_key:
        fail("missing_row_binding", "A nonempty typed source identity is required")
    counter = _Budget(budget)
    for text in (binding.resource_id, binding.source_generation, binding.schema_fingerprint):
        counter.string(text)
    for name, identity in binding.identity_key.items():
        counter.visit(0)
        if (
            type(name) is not str
            or not name
            or type(identity) is not dict
            or set(identity) != {"type", "value"}
            or type(identity["type"]) is not str
            or not identity["type"]
            or type(identity["value"]) is not str
        ):
            fail("missing_row_binding", "Row identity must use canonical typed-v1 scalar metadata")
        for text in (name, identity["type"], identity["value"]):
            counter.string(text)
    try:
        material = json.dumps(
            vars(binding), ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
        )
        material_size = len(material.encode("utf-8"))
    except (TypeError, ValueError, UnicodeError, RecursionError):
        fail("missing_row_binding", "Row binding must be canonical JSON metadata")
    if material_size > budget.max_bytes:
        fail("value_budget", "Row binding exceeds byte budget")
    return material


def _complete(value, code="incomplete_row"):
    if type(value) is FieldValue:
        if value.state in {ValueState.MISSING, ValueState.UNAVAILABLE}:
            fail(
                code,
                "Rows must be completely materialized, including nested fields",
            )
        _complete(value.value, code)
    elif type(value) is dict:
        for key, item in value.items():
            _complete(key, code)
            _complete(item, code)
    elif type(value) in {list, tuple}:
        for item in value:
            _complete(item, code)


def merge_row(
    after: DecodedRow,
    *,
    operation: str,
    binding: RowBinding,
    previous: DecodedRow | None = None,
    previous_binding: RowBinding | None = None,
    budget: ValueBudget = DEFAULT_BUDGET,
) -> DecodedRow:
    """Resolve only top-level unchanged TOAST fields for an update.

    Callers must load previous from durable, ordered state and bind the reviewed
    source schema/identity themselves. Passing bindings cannot establish that
    authority. Missing fields, changed schema and unsupported ops never guess.
    Returned data is detached from both inputs.
    """
    if type(operation) is not str or operation not in {"r", "c", "u"}:
        fail("unsupported_merge_operation", "Only snapshot/create/update after-images can be materialized")
    current_binding = _binding(binding, budget)
    current = decode_row(encode_row(after, budget=budget), budget=budget)
    prior = None
    if previous is not None:
        if previous_binding is None or _binding(previous_binding, budget) != current_binding:
            fail(
                "incompatible_previous_row",
                "Previous row belongs to a different resource/schema/identity/generation",
            )
        prior = decode_row(encode_row(previous, budget=budget), budget=budget)
        _complete(prior.fields, "incomplete_previous_row")
        if set(prior.fields) != set(current.fields):
            fail("incompatible_previous_row", "Rows bound to the same schema must have the same field set")
    elif previous_binding is not None:
        fail("missing_previous_row", "A binding alone is not previous source data")
    merged = {}
    for name, field in current.fields.items():
        if field.state == ValueState.MISSING:
            fail("missing_source_field", "Missing fields are not NULL and cannot be recovered by guessing")
        if field.state == ValueState.UNAVAILABLE:
            if operation != "u" or prior is None:
                fail(
                    "unavailable_without_previous", "Unchanged TOAST requires a complete previous update row"
                )
            # NULL is not a previous material value from which TOAST can recover.
            if prior.fields[name].state != ValueState.VALUE:
                fail("unavailable_without_previous", "Unchanged TOAST requires a known prior field value")
            merged[name] = prior.fields[name]
        else:
            merged[name] = field
    result = DecodedRow(merged)
    _complete(result.fields)
    # Combining individually bounded rows must not bypass the aggregate limit.
    return decode_row(encode_row(result, budget=budget), budget=budget)


def _mapping_value(value):
    if type(value) is FieldValue:
        if value.state in {ValueState.MISSING, ValueState.UNAVAILABLE}:
            fail("incomplete_mapping_value", "Missing/unavailable values cannot be mapped as NULL")
        return _mapping_value(value.value)
    if value is None or type(value) in {str, bool, int, float}:
        return value
    if type(value) is Decimal:
        return str(value)  # Explicit finite Decimal conversion, never float or context rounding.
    if type(value) is UUID:
        return str(value)
    if type(value) in {date, datetime}:
        return value.isoformat()
    if type(value) is list:
        return [_mapping_value(item) for item in value]
    if type(value) is dict:
        if any(type(key) is not str for key in value):
            fail(
                "unsupported_mapping_type",
                "JSON mapping needs string map keys; no coercion or flattening is implicit",
            )
        return {key: _mapping_value(item) for key, item in value.items()}
    fail(
        "unsupported_mapping_type",
        "Bytes, time, exact temporal and typed map pairs need an explicit mapping policy",
    )


def mapping_values(
    row: DecodedRow, *, fields: Iterable[str] | None = None, budget: ValueBudget = DEFAULT_BUDGET
) -> dict:
    """Convert selected complete fields to ordinary JSON with explicit type rules.

    Integers remain Python ints, Decimal becomes its exact decimal string, UUID
    its standard string, and date/datetime ISO text. Unsupported mapped types
    fail; an unselected binary field does not block an integer/text mapping.
    """
    if type(row) is not DecodedRow or type(row.fields) is not dict:
        fail()
    if isinstance(fields, (str, bytes)):
        fail("invalid_field_selection", "Select field names as an iterable, not a string")
    selected, seen = [], set()
    for name in row.fields if fields is None else fields:
        if len(selected) >= budget.max_values:
            fail("value_budget", "Field selection exceeds value budget")
        if type(name) is not str or name in seen:
            fail("invalid_field_selection", "Field selection must be unique strings")
        if name not in row.fields:
            fail("missing_source_field", "Selected field is absent")
        selected.append(name)
        seen.add(name)
    # Validate and bound only referenced fields; unselected native types remain
    # available in their durable typed row without implicitly coercing them.
    subset = DecodedRow({name: row.fields[name] for name in selected})
    checked = decode_row(encode_row(subset, budget=budget), budget=budget)
    result = {name: _mapping_value(value) for name, value in checked.fields.items()}
    _json_size(result, budget)
    return result
