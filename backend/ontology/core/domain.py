import json
import re
from datetime import date, datetime
from decimal import Decimal, InvalidOperation

from sqlalchemy import select, update

from ontology.core.errors import DomainError, conflict, missing
from ontology.identity.models import Project
from ontology.identity.security import ROLES, allowed_field
from ontology.modeling.models import Release
from ontology.objects.models import EntityRevision


def row_dict(row, exclude=()):
    return {
        column.name: getattr(row, column.name)
        for column in row.__table__.columns
        if column.name not in exclude
    }


def wire_value(value):
    """Preserve exact integers across JavaScript JSON parsers; storage is unchanged."""
    if isinstance(value, int) and not isinstance(value, bool) and abs(value) > 2**53 - 1:
        return str(value)
    if isinstance(value, dict):
        return {key: wire_value(item) for key, item in value.items()}
    if isinstance(value, list):
        return [wire_value(item) for item in value]
    return value


def exact_json_text(value):
    """Browser display of raw evidence; preserve integers and document key order."""
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def active_definition(session, project: Project) -> dict:
    release = session.get(Release, project.active_release_id) if project.active_release_id else None
    return release.definition if release else {"types": []}


def type_definition(session, project, key):
    definition = active_definition(session, project)
    for item in definition["types"]:
        if item["key"] == key:
            return item
    raise DomainError(422, "unknown_type", "对象类型尚未发布")


def coerce_value(prop, value):
    if value is None:
        if prop.get("required"):
            raise ValueError("必填字段不能为 null")
        return None
    kind = prop["data_type"]
    if kind == "string":
        if not isinstance(value, str) or len(value) > 100000:
            raise ValueError("应为文本且不超过 100000 字符")
    elif kind == "integer":
        if isinstance(value, str):
            if len(value) > 1001 or not re.fullmatch(r"[+-]?[0-9]+", value):
                raise ValueError("整数文本须为十进制数字，不能含小数或指数，长度不得超过 1000 位")
            value = int(value)
        if not isinstance(value, int) or isinstance(value, bool):
            raise ValueError("应为整数")
        if len(str(abs(value))) > 1000:
            raise ValueError("整数长度不得超过 1000 位")
    elif kind == "number":
        if isinstance(value, bool) or not isinstance(value, (int, float, str)):
            raise ValueError("应为十进制数")
        try:
            decimal = Decimal(str(value))
            if not decimal.is_finite():
                raise ValueError("数值必须有限")
            value = str(decimal)
        except InvalidOperation as exc:
            raise ValueError("无效十进制数") from exc
    elif kind == "boolean":
        if not isinstance(value, bool):
            raise ValueError("应为布尔值")
    elif kind == "enum":
        if value not in prop.get("enum", []):
            raise ValueError("不在已定义枚举中")
    elif kind == "date":
        if not isinstance(value, str):
            raise ValueError("应为 ISO 日期")
        date.fromisoformat(value)
    elif kind == "datetime" and (not isinstance(value, str) or datetime.fromisoformat(value).tzinfo is None):
        raise ValueError("时间必须包含时区")
    return value


def validate_properties(type_def, values, *, partial=False):
    props = {item["key"]: item for item in type_def.get("properties", [])}
    errors, result = [], {}
    for key in values:
        if key not in props:
            errors.append({"field": key, "message": "未定义属性"})
    for key, prop in props.items():
        if key not in values:
            if prop.get("required") and not partial:
                errors.append({"field": key, "message": "缺少必填属性"})
            continue
        try:
            result[key] = coerce_value(prop, values[key])
        except (ValueError, TypeError) as exc:
            errors.append({"field": key, "message": str(exc)})
    if errors:
        raise DomainError(422, "invalid_properties", "属性校验失败", errors)
    return result


def validate_source_properties(type_def, values):
    """Validate materialized source values while retaining explicit required NULL.

    Source-owned NULL is evidence of an incomplete object, not a reason to
    fabricate a value or withdraw the contribution. This helper does not mutate
    the published required contract; callers still use it to derive data status.
    Present manual fields and every non-null value keep their normal validation.
    Manual write APIs must continue calling validate_properties directly.
    """
    source_view = {
        **type_def,
        "properties": [
            {**prop, "required": False} if prop.get("source_owned") else prop
            for prop in type_def.get("properties", [])
        ],
    }
    return validate_properties(source_view, values, partial=True)


def definition_errors(definition):
    errors = []
    keys = [item["key"] for item in definition["types"]]
    if len(set(keys)) != len(keys):
        errors.append({"field": "types", "message": "类型标识重复"})
    for item in definition["types"]:
        for collection in ("properties", "relations"):
            names = [p["key"] for p in item.get(collection, [])]
            if len(set(names)) != len(names):
                errors.append({"field": f"{item['key']}.{collection}", "message": "标识重复"})
        for prop in item.get("properties", []):
            if prop["data_type"] == "enum" and not prop.get("enum"):
                errors.append({"field": prop["key"], "message": "枚举必须定义选项"})
            if (set(prop.get("read_roles", [])) | set(prop.get("write_roles", []))) - set(ROLES):
                errors.append({"field": prop["key"], "message": "存在未知角色"})
        props = {p["key"]: p for p in item.get("properties", [])}
        seen_keys = set()
        for fields in item.get("unique_keys", []):
            signature = tuple(sorted(fields))
            if (
                not fields
                or len(fields) > 16
                or len(set(fields)) != len(fields)
                or signature in seen_keys
                or any(f not in props or props[f]["data_type"] == "json" for f in fields)
            ):
                errors.append(
                    {"field": item["key"] + ".unique_keys", "message": "唯一键须为不重复的已定义标量字段组合"}
                )
            seen_keys.add(signature)
        for relation in item.get("relations", []):
            if relation["target_type"] not in keys:
                errors.append({"field": relation["key"], "message": "关系目标类型不存在"})
    return errors


def scoped_record(session, model, record_id, user):
    record = session.scalar(select(model).where(model.id == record_id, model.tenant_id == user.tenant_id))
    if record is None:
        missing()
    return record


def compare_and_swap(session, record, expected_revision, values):
    model = type(record)
    result = session.execute(
        update(model)
        .where(
            model.id == record.id, model.tenant_id == record.tenant_id, model.revision == expected_revision
        )
        .values(**values, revision=expected_revision + 1)
        .execution_options(synchronize_session=False)
    )
    if result.rowcount != 1:
        conflict(details={"expected_revision": expected_revision})
    session.expire(record)
    session.refresh(record)
    return record


def visible_evidence(references, visible):
    result = []
    for reference in references:
        if "fields" in reference:
            fields = [field for field in reference["fields"] if field in visible]
            if fields:
                item = {**reference, "fields": fields}
                if "source_record_id" in item or len(fields) != len(reference["fields"]):
                    item.pop("content_hash", None)
                result.append(item)
        elif "source_record_id" not in reference:
            result.append(reference)
        # Legacy source references without property provenance cannot safely
        # disclose the full row's lineage to a field-limited object reader.
    return result


def entity_view(session, obj, access, *, definition=None):
    # A collection may reuse this request's immutable release definition. Field
    # authorization and wire conversion still run for every returned object.
    if definition is None:
        definition = type_definition(session, access.project, obj.type_key)
    elif definition.get("key") != obj.type_key:
        raise ValueError("The supplied definition does not match the object's type")
    visible = {p["key"] for p in definition.get("properties", []) if allowed_field(p, access.role)}
    result = row_dict(obj)
    # release_id remains the object's last-write provenance. These fields name
    # the immutable model used to interpret/filter this particular response.
    result["model_release_id"] = access.project.active_release_id
    result["model_version"] = access.project.version
    result["properties"] = wire_value({key: value for key, value in obj.properties.items() if key in visible})
    result["evidence"] = visible_evidence(obj.evidence, visible)
    return result


def save_entity_revision(session, obj, user, reason=""):
    from ontology.search.objects.index import enqueue_entity

    session.add(
        EntityRevision(
            tenant_id=obj.tenant_id,
            project_id=obj.project_id,
            entity_id=obj.id,
            revision=obj.revision,
            properties=obj.properties,
            evidence=obj.evidence,
            actor_id=user.id,
            reason=reason,
            data_status=obj.data_status,
        )
    )
    from ontology.objects.constraints import sync_entity_keys, touch_entity

    sync_entity_keys(session, obj)
    touch_entity(session, obj)
    enqueue_entity(session, obj)
    from ontology.objects.relation_rules import changed_entity

    changed_entity(session, obj)
