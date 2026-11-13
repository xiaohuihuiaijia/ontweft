"""Explain model changes without introducing a second publication policy."""

from ontology.modeling.schemas import TypeDefinition


def normalized_type(value):
    # Old releases predate canonical key semantics and optional fields.
    return TypeDefinition.model_validate(
        {**value, "key_semantics": value.get("key_semantics", "legacy")}
    ).model_dump()


def normalized_mapping(value):
    return {
        **value,
        "identity_fields": value.get("identity_fields", []),
        "fields": {key: {"transform": "identity", **field} for key, field in value.get("fields", {}).items()},
    }


def definition_diff(previous, current):
    old = {value["key"]: normalized_type(value) for value in previous.get("types", [])}
    new = {value["key"]: normalized_type(value) for value in current.get("types", [])}
    items = []

    def add(kind, type_key, key, change, risk, label, message):
        items.append(
            {
                "kind": kind,
                "type_key": type_key,
                "key": key,
                "change": change,
                "risk": risk,
                "label": label,
                "message": message,
            }
        )

    def fields(type_key, before, after, relation=None):
        left, right = ({value["key"]: value for value in group} for group in (before, after))
        kind = "relation_property" if relation else "property"
        for key in sorted(left.keys() | right.keys()):
            old_field, new_field = left.get(key), right.get(key)
            path = f"{relation}.{key}" if relation else key
            field = new_field or old_field
            if old_field is None:
                add(
                    kind,
                    type_key,
                    path,
                    "added",
                    "review" if field["required"] else "compatible",
                    field["label"],
                    "新增必填字段，需核对已有记录" if field["required"] else "新增可选字段",
                )
            elif new_field is None:
                add(kind, type_key, path, "removed", "review", field["label"], "删除字段，需核对已有值与映射")
            elif old_field != new_field:
                changed = {name for name in old_field if old_field[name] != new_field[name]}
                display = changed <= {"label"}
                add(
                    kind,
                    type_key,
                    path,
                    "modified",
                    "display" if display else "review",
                    field["label"],
                    "修改字段显示名称" if display else "修改字段类型、约束、单位或权限，需校验已有值",
                )

    for key in sorted(old.keys() | new.keys()):
        before, after = old.get(key), new.get(key)
        value = after or before
        if before is None or after is None:
            add(
                "type",
                key,
                key,
                "added" if before is None else "removed",
                "compatible" if before is None else "review",
                value["label"],
                "新增业务类型" if before is None else "删除类型，需核对现有对象与引用",
            )
            continue
        if any(before[name] != after[name] for name in ("label", "description", "aliases")):
            add("type", key, key, "modified", "display", after["label"], "修改名称、说明或别名；稳定标识未变")
        if any(before[name] != after[name] for name in ("unique_keys", "key_semantics")):
            add(
                "type",
                key,
                key,
                "modified",
                "review",
                after["label"],
                "修改唯一键或身份比较方式，需校验现有对象",
            )
        fields(key, before["properties"], after["properties"])
        left, right = ({r["key"]: r for r in value["relations"]} for value in (before, after))
        for rel_key in sorted(left.keys() | right.keys()):
            old_rel, new_rel = left.get(rel_key), right.get(rel_key)
            relation = new_rel or old_rel
            if old_rel is None or new_rel is None:
                add(
                    "relation",
                    key,
                    rel_key,
                    "added" if old_rel is None else "removed",
                    "compatible" if old_rel is None else "review",
                    relation["label"],
                    "新增关系定义" if old_rel is None else "删除关系定义，需核对现有关系",
                )
                continue
            changed = {
                name for name in ("label", "target_type", "cardinality") if old_rel[name] != new_rel[name]
            }
            if changed:
                risk = (
                    "display"
                    if changed == {"label"}
                    else "compatible"
                    if changed == {"cardinality"} and new_rel["cardinality"] == "many"
                    else "review"
                )
                add(
                    "relation",
                    key,
                    rel_key,
                    "modified",
                    risk,
                    relation["label"],
                    "修改关系显示名称" if risk == "display" else "修改目标类型或关系数量限制",
                )
            fields(key, old_rel["properties"], new_rel["properties"], rel_key)

    old_mappings, new_mappings = (
        {value["key"]: normalized_mapping(value) for value in definition.get("mappings", [])}
        for definition in (previous, current)
    )
    for key in sorted(old_mappings.keys() | new_mappings.keys()):
        before, after = old_mappings.get(key), new_mappings.get(key)
        if before != after:
            value = after or before
            add(
                "mapping",
                value["type_key"],
                key,
                "added" if before is None else "removed" if after is None else "modified",
                "review",
                key,
                "来源映射变化，需核对来源字段、身份和对象投影",
            )
    affected_types = {item["type_key"] for item in items if item["risk"] != "display"}
    affected_types.update(
        old_mappings[item["key"]]["type_key"]
        for item in items
        if item["kind"] == "mapping" and item["key"] in old_mappings
    )
    affected_mappings = {
        key for key, value in (old_mappings | new_mappings).items() if value["type_key"] in affected_types
    }
    return {
        "items": items,
        "counts": {
            kind: sum(item["change"] == kind for item in items) for kind in ("added", "modified", "removed")
        },
        "affected_type_keys": sorted(affected_types),
        "affected_mapping_keys": sorted(affected_mappings),
    }
