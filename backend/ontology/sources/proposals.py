"""Deterministic, editable schema proposals; never auto-publish or infer business facts."""

import copy
import re

from ontology.core.errors import DomainError
from ontology.modeling.schemas import Definition
from ontology.sources.ingestion import schema_fingerprint


def _key(value, fallback, used):
    value = re.sub(r"[^a-zA-Z0-9_]", "_", value).strip("_")[:64]
    if not value or not value[0].isalpha():
        value = fallback
    original, number = value, 2
    while value in used:
        value, number = f"{original}_{number}", number + 1
    used.add(value)
    return value


def suggested_type(field):
    suggested = field.get("metadata", {}).get("suggested_type")
    if suggested in {"string", "integer", "number", "boolean"}:
        return suggested
    raw = field.get("data_type", "").lower()
    if re.search(r"\b(?:tinyint|smallint|bigint|integer|int|int[248]|serial|bigserial)\b", raw):
        return "integer"
    if re.search(r"\b(?:numeric|decimal|real|double|float|number)\b", raw):
        return "number"
    if raw in {"bool", "boolean"}:
        return "boolean"
    if raw == "date":
        return "date"
    if raw in {"json", "jsonb", "object", "array"}:
        return "json"
    return "string"


def propose(source, resource_ids, current):
    definition = copy.deepcopy(current)
    definition.setdefault("mappings", [])
    resources = {r["resource_id"]: r for r in source.resources}
    if len(set(resource_ids)) != len(resource_ids) or any(r not in resources for r in resource_ids):
        raise DomainError(422, "invalid_resources", "请选择不重复的已发现资源")
    type_keys = {t["key"] for t in definition["types"]}
    mapping_keys = {m["key"] for m in definition["mappings"]}
    summaries = []
    for index, resource_id in enumerate(resource_ids):
        resource = resources[resource_id]
        if not resource.get("primary_key"):
            raise DomainError(
                422, "stable_identity_required", "资源尚未声明稳定主键，请先在来源中定义记录身份"
            )
        if any(
            m["source_id"] == source.id and m["resource_id"] == resource_id for m in definition["mappings"]
        ):
            raise DomainError(409, "resource_already_mapped", "资源已有发布映射，请在本体模型中编辑原有映射")
        fields = [f for f in resource.get("fields", []) if not f.get("metadata", {}).get("technical")]
        if not 1 <= len(fields) <= 200:
            raise DomainError(422, "unsupported_resource", "建模资源须有 1–200 个业务字段")
        label = resource["name"][:120]
        key = _key(resource["name"], f"Table_{source.id[:8].replace('-', '')}_{index + 1}", type_keys)
        name_field = next(
            (
                f["name"]
                for f in fields
                if re.search(
                    r"(?i)^(?:name|title|名称|名字|标题|.*名称)$",
                    f.get("metadata", {}).get("label", f["name"]),
                )
            ),
            None,
        )
        props, mappings, used = [], {}, {"name"} if name_field else set()
        for number, field in enumerate(fields):
            prop_key = (
                "name" if field["name"] == name_field else _key(field["name"], f"field_{number + 1}", used)
            )
            kind = suggested_type(field)
            props.append(
                {
                    "key": prop_key,
                    "label": field.get("metadata", {}).get("label", field["name"])[:120],
                    "data_type": kind,
                    "required": False,
                    "source_owned": True,
                }
            )
            mappings[prop_key] = {
                "source_field": field["name"],
                "transform": kind if kind in {"integer", "number", "boolean", "date"} else "identity",
            }
        definition["types"].append(
            {
                "key": key,
                "label": label,
                "description": "由来源结构生成的草稿，请确认业务含义、展示名称与字段类型。",
                "properties": props,
                "relations": [],
            }
        )
        definition["mappings"].append(
            {
                "key": _key(f"map_{key}", f"mapping_{index + 1}", mapping_keys),
                "source_id": source.id,
                "resource_id": resource_id,
                "type_key": key,
                "schema_fingerprint": schema_fingerprint(resource),
                "fields": mappings,
            }
        )
        summaries.append(
            {
                "resource_id": resource_id,
                "type_key": key,
                "label": label,
                "primary_key": resource["primary_key"],
                "metadata": resource.get("metadata", {}),
                "declared_foreign_keys": resource.get("foreign_keys", []),
            }
        )
    if len(definition["types"]) > 500 or len(definition["mappings"]) > 500:
        raise DomainError(422, "model_budget", "新增后将超出模型预算，请减少本次资源数量")
    return {
        "definition": Definition.model_validate(definition).model_dump(),
        "resources": summaries,
        "warnings": [
            "仅生成可编辑草稿；请确认业务名称、字段类型和显示名称。",
            "未推断必填或唯一性约束；声明外键仅作为关系设计依据，不自动创建对象连线。",
            "上传行号只在本次文件内稳定；再次上传会成为独立来源，不自动合并对象。",
        ],
    }
