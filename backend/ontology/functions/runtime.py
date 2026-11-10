"""Trusted, bounded Python kernels. No user code or interpreter is accepted.

The arithmetic kernel receives three immutable quantities only: no Session,
credentials, object loader, filesystem handle, tool registry, or network client.
This is a trusted application function, not an untrusted-code sandbox.
"""

import hashlib
import inspect
import json
import platform
from decimal import Decimal, localcontext
from importlib.metadata import version as package_version
from pathlib import Path
from typing import Literal

from pydantic import Field, ValidationError, field_serializer, field_validator
from sqlalchemy import select

from .domain import active_definition, visible_evidence
from .errors import DomainError, conflict, forbidden, missing
from .models import Entity, Project, User
from .schemas import StrictModel
from .security import access, allowed_field

KERNEL_KEY = "inventory_gap"
KERNEL_VERSION = 1
INPUT_NAMES = ("demand", "inventory", "reserved")
MODULE_SHA256 = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()


def digest(value):
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    ).hexdigest()


def decimal_text(value):
    if not value:
        return "0"
    text = format(value, "f")
    return text.rstrip("0").rstrip(".") if "." in text else text


class InventoryInput(StrictModel):
    demand: Decimal = Field(ge=0, le=10**18, max_digits=28, decimal_places=9, allow_inf_nan=False)
    inventory: Decimal = Field(ge=0, le=10**18, max_digits=28, decimal_places=9, allow_inf_nan=False)
    reserved: Decimal = Field(ge=0, le=10**18, max_digits=28, decimal_places=9, allow_inf_nan=False)

    @field_validator("*", mode="before")
    @classmethod
    def bounded_exact_input(cls, value):
        if isinstance(value, bool) or not isinstance(value, (str, int, Decimal)):
            raise ValueError("需要精确十进制字符串或整数，不能使用浮点、null 或布尔值")  # noqa: TRY004 - Pydantic requires ValueError for a validation result
        if len(str(value)) > 64:
            raise ValueError("数值表示超出预算")
        return value

    @field_serializer("*")
    def serialize_decimal(self, value):
        return decimal_text(value)


class InventoryResult(StrictModel):
    available: Decimal = Field(ge=0, le=10**18, max_digits=28, decimal_places=9, allow_inf_nan=False)
    gap: Decimal = Field(ge=0, le=10**18, max_digits=28, decimal_places=9, allow_inf_nan=False)

    @field_serializer("*")
    def serialize_decimal(self, value):
        return decimal_text(value)


def inventory_gap(inputs: InventoryInput) -> InventoryResult:
    with localcontext() as context:
        context.prec = 40
        available = max(inputs.inventory - inputs.reserved, Decimal(0))
        return InventoryResult(available=available, gap=max(inputs.demand - available, Decimal(0)))


def self_tests():
    cases = [
        ({"demand": "100", "inventory": "80", "reserved": "20"}, {"available": "60", "gap": "40"}),
        ({"demand": "0.3", "inventory": "0.3", "reserved": "0.2"}, {"available": "0.1", "gap": "0.2"}),
        ({"demand": "4", "inventory": "1", "reserved": "2"}, {"available": "0", "gap": "4"}),
    ]
    for inputs, expected in cases:
        if inventory_gap(InventoryInput.model_validate(inputs)).model_dump(mode="json") != expected:
            raise DomainError(503, "function_self_test_failed", "内置函数自测未通过")
    return {"passed": len(cases), "cases_sha256": digest(cases)}


def catalog_entry():
    return {
        "key": KERNEL_KEY,
        "version": KERNEL_VERSION,
        "language": "python",
        "trust": "reviewed_application_builtin",
        "side_effects": "read_only_business_data",
        "implementation_sha256": MODULE_SHA256,
        "implementation_source": inspect.getsource(inventory_gap),
        "dependencies": {
            "python": platform.python_version(),
            "pydantic": package_version("pydantic"),
            "stdlib": ["decimal"],
        },
        "input_schema": InventoryInput.model_json_schema(),
        "output_schema": InventoryResult.model_json_schema(),
        "budget": {
            "object_reads": 3,
            "decimal_places": 9,
            "maximum_quantity": "1000000000000000000",
            "dynamic_code": False,
            "tool_calls": 0,
        },
        "formula": {"available": "max(inventory - reserved, 0)", "gap": "max(demand - available, 0)"},
        "null_policy": "reject_missing_or_null",
        "self_tests": self_tests(),
    }


class FieldBinding(StrictModel):
    type_key: str = Field(min_length=1, max_length=120)
    field: str = Field(min_length=1, max_length=80)


class FunctionInput(StrictModel):
    project_id: str
    key: str = Field(pattern=r"^[A-Za-z][A-Za-z0-9_]{0,79}$")
    label: str = Field(min_length=1, max_length=200)
    kernel_key: Literal["inventory_gap"] = KERNEL_KEY
    kernel_version: Literal[1] = KERNEL_VERSION
    baseline_release_id: str
    unit: str = Field(min_length=1, max_length=80)
    bindings: dict[str, FieldBinding] = Field(min_length=3, max_length=3)


class InputObject(StrictModel):
    object_id: str
    expected_revision: int = Field(ge=1)


class FunctionExecution(StrictModel):
    expected_function_revision: int = Field(ge=1)
    objects: dict[str, InputObject] = Field(min_length=3, max_length=3)


def validate_bindings(session, ctx, bindings, unit):
    if set(bindings) != set(INPUT_NAMES):
        raise DomainError(422, "function_input_mismatch", "必须分别绑定 demand、inventory、reserved")
    types = {item["key"]: item for item in active_definition(session, ctx.project)["types"]}
    for binding in bindings.values():
        type_def = types.get(binding["type_key"], {})
        prop = next((p for p in type_def.get("properties", []) if p["key"] == binding["field"]), None)
        if not prop or not allowed_field(prop, ctx.role):
            forbidden()
        if prop["data_type"] not in {"integer", "number"} or prop.get("unit") != unit:
            raise DomainError(422, "function_binding_type", "函数输入必须为数值且有相同的显式单位")


def definition_hash(row):
    return digest(
        {
            "project_id": row.project_id,
            "key": row.key,
            "version": row.version,
            "kernel_key": row.kernel_key,
            "kernel_version": row.kernel_version,
            "bindings": row.bindings,
            "unit": row.unit,
            "baseline_release_id": row.baseline_release_id,
            "artifact": row.artifact,
        }
    )


def check_current_definition(session, ctx, row):
    if row.baseline_release_id != ctx.project.active_release_id:
        conflict("本体基线已改变，请登记新函数版本并重新审核")
    if row.artifact != catalog_entry() or row.snapshot_hash != definition_hash(row):
        conflict("函数实现或配置摘要已改变，请重新登记与审核")
    validate_bindings(session, ctx, row.bindings, row.unit)


def read_access(session, user, project_id):
    # Bounded reads hold the same project/identity order as other mutations.
    # A shared project lock coordinates model, member and object changes.
    session.scalar(
        select(Project)
        .where(Project.id == project_id, Project.tenant_id == user.tenant_id)
        .with_for_update(read=True)
    )
    session.scalar(
        select(User)
        .where(User.id == user.id, User.tenant_id == user.tenant_id)
        .with_for_update(key_share=True)
    )
    return access(session, user, project_id)


def read_inputs(session, ctx, row, objects):
    if set(objects) != set(INPUT_NAMES):
        raise DomainError(422, "function_input_mismatch", "输入对象与函数参数不匹配")
    inputs = {}
    for name in INPUT_NAMES:
        requested, binding = objects[name], row.bindings[name]
        obj = session.scalar(
            select(Entity).where(
                Entity.id == requested.object_id,
                Entity.tenant_id == ctx.user.tenant_id,
                Entity.project_id == ctx.project.id,
                Entity.deleted.is_(False),
            )
        )
        if obj is None or obj.type_key != binding["type_key"]:
            missing()
        if obj.revision != requested.expected_revision:
            conflict("函数输入对象版本已变化")
        field = binding["field"]
        if field not in obj.properties or obj.properties[field] is None:
            raise DomainError(422, "function_missing_input", "函数输入缺失或为 null，不能视为零")
        # Source evidence declaring fields only supports those exact fields.
        evidence = visible_evidence(obj.evidence, {field})
        inputs[name] = {
            "object_id": obj.id,
            "revision": obj.revision,
            "type_key": obj.type_key,
            "field": field,
            "value": obj.properties[field],
            "evidence": evidence,
        }
    try:
        parsed = InventoryInput.model_validate({key: value["value"] for key, value in inputs.items()})
    except ValidationError as exc:
        raise DomainError(422, "function_invalid_input", "函数输入超出类型、精度或数值预算") from exc
    return inputs, parsed


def authorize_history(session, ctx, run):
    bindings = {
        name: {"type_key": item["type_key"], "field": item["field"]} for name, item in run.inputs.items()
    }
    validate_bindings(session, ctx, bindings, run.result["unit"])
    for item in run.inputs.values():
        obj = session.scalar(
            select(Entity).where(
                Entity.id == item["object_id"],
                Entity.tenant_id == ctx.user.tenant_id,
                Entity.project_id == ctx.project.id,
                Entity.deleted.is_(False),
            )
        )
        if obj is None or obj.type_key != item["type_key"]:
            missing()
