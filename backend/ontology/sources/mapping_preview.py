"""Read-only, bounded previews of saved mappings against persisted source rows.

These are selected-sample diagnostics, not projection or whole-source validation.
No connector, current entity, identity resolver or publication path is consulted.
"""

from decimal import InvalidOperation
from typing import Annotated

from fastapi import APIRouter, Depends, Request
from pydantic import Field, ValidationError, field_validator
from sqlalchemy import select

from ontology.core.domain import definition_errors, exact_json_text, scoped_record, validate_properties
from ontology.core.errors import DomainError, conflict, missing
from ontology.core.schemas import StrictModel
from ontology.identity.security import access, authenticated
from ontology.modeling.models import ChangeSet
from ontology.modeling.schemas import Definition
from ontology.sources.ingestion import fingerprint, mapped_properties, schema_fingerprint, transform_value
from ontology.sources.models import Source, SourceRecordRevision, SourceSnapshot

router = APIRouter()
MAX_PREVIEW_BYTES = 2_000_000


class MappingPreviewInput(StrictModel):
    changeset_id: str = Field(min_length=1, max_length=36)
    expected_revision: int = Field(ge=1, strict=True)
    mapping_key: str = Field(pattern=r"^[a-zA-Z][a-zA-Z0-9_]{0,79}$")
    snapshot_id: str = Field(min_length=1, max_length=36)
    expected_snapshot_attempt: int = Field(ge=1, strict=True)
    record_ids: list[Annotated[str, Field(min_length=1, max_length=36)]] = Field(min_length=1, max_length=20)

    @field_validator("record_ids")
    @classmethod
    def distinct_records(cls, value):
        if len(set(value)) != len(value):
            raise ValueError("记录标识必须唯一")
        return value


def error(stage, code, message):
    return {"stage": stage, "code": code, "message": message}


def preview_record(record, mapping, type_def, budget):
    fields, row_errors = [], []

    def encoded(value):
        text = exact_json_text(value)
        budget[0] += len(text.encode("utf-8"))
        if budget[0] > MAX_PREVIEW_BYTES:
            raise DomainError(422, "preview_budget", "选中样本内容超过预览预算，请减少样本或映射字段")
        return text

    for target, binding in mapping["fields"].items():
        source_field = binding["source_field"]
        state = (
            "missing"
            if source_field not in record.values
            else ("null" if record.values[source_field] is None else "value")
        )
        field = {
            "target_field": target,
            "source_field": source_field,
            "transform": binding["transform"],
            "source_state": state,
            "raw_value_json": None,
            "transformed_value_json": None,
            "validated_value_json": None,
            "errors": [],
        }
        fields.append(field)
        if state == "missing":
            field["errors"].append(
                error("source", "missing_source_field", "源记录缺少映射字段；缺失值不能当作 null")
            )
            continue
        raw = record.values[source_field]
        field["raw_value_json"] = encoded(raw)
        try:
            converted = transform_value(raw, binding["transform"])
        except (ValueError, TypeError, InvalidOperation, OverflowError):
            field["errors"].append(
                error("transform", "mapping_conversion_failed", "来源转换失败，请检查原始值与转换规则")
            )
            continue
        field["transformed_value_json"] = encoded(converted)
        try:
            validated = validate_properties(type_def, {target: converted}, partial=True)
        except DomainError as exc:
            field["errors"].extend(error("type", exc.code, item["message"]) for item in exc.details)
            continue
        field["validated_value_json"] = encoded(validated[target])

    properties = None
    if not any(field["errors"] for field in fields):
        # Use the same complete mapping function as actual projection after
        # collecting all field diagnostics; never merge current entity values.
        properties = mapped_properties(record, mapping, type_def)
        try:
            properties = validate_properties(type_def, properties)
        except DomainError as exc:
            row_errors = [
                {"field": item["field"], **error("type", exc.code, item["message"])} for item in exc.details
            ]
            properties = None
    valid = not row_errors and not any(field["errors"] for field in fields)
    return {
        "record_id": record.id,
        "valid": valid,
        "fields": fields,
        "properties_json": encoded(properties) if valid else None,
        "errors": row_errors,
    }


def preview(session, user, data: MappingPreviewInput):
    change = scoped_record(session, ChangeSet, data.changeset_id, user)
    # Existing writers/membership updates use the same project lock. Refresh
    # the loaded change after acquisition so a prior revision is never reused.
    ctx = access(session, user, change.project_id, "ontology:write")
    access(session, user, change.project_id, "sources:write")
    session.refresh(change)
    if change.revision != data.expected_revision or change.status not in {"draft", "validated", "approved"}:
        conflict("变更集修订或状态已改变，请重新保存并选择预览")
    if change.baseline_release_id != ctx.project.active_release_id:
        conflict("本体发布基线已改变，请基于当前模型重新保存变更集")
    try:
        definition = Definition.model_validate(change.definition).model_dump()
    except ValidationError as exc:
        raise DomainError(422, "mapping_invalid", "已保存变更集结构无效，请修订草稿") from exc
    schema_errors = definition_errors(definition)
    mappings = [row for row in definition["mappings"] if row["key"] == data.mapping_key]
    if not mappings:
        missing()
    if len(mappings) != 1:
        schema_errors.append({"mapping": data.mapping_key, "message": "映射标识重复"})
    mapping = mappings[0]
    type_def = next((row for row in definition["types"] if row["key"] == mapping["type_key"]), None)
    if type_def is None:
        schema_errors.append({"mapping": data.mapping_key, "message": "目标对象类型未定义"})
    if schema_errors:
        raise DomainError(422, "mapping_invalid", "映射结构未通过校验", schema_errors)

    source = session.scalar(
        select(Source).where(
            Source.id == mapping["source_id"],
            Source.tenant_id == user.tenant_id,
            Source.project_id == change.project_id,
        )
    )
    snapshot = session.scalar(
        select(SourceSnapshot)
        .where(
            SourceSnapshot.id == data.snapshot_id,
            SourceSnapshot.tenant_id == user.tenant_id,
            SourceSnapshot.project_id == change.project_id,
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if source is None or snapshot is None:
        missing()
    if source.kind == "feishu":
        raise DomainError(422, "employee_authorization_required", "飞书来源预览须先配置当前员工授权服务")
    if snapshot.source_id != source.id or snapshot.resource_id != mapping["resource_id"]:
        conflict("快照与所选映射的来源或资源不匹配")
    if snapshot.attempt != data.expected_snapshot_attempt:
        conflict("快照采集尝试已改变，请刷新并重新选择记录")
    if snapshot.status not in {"complete", "failed"} or snapshot.attempt <= 0:
        raise DomainError(422, "snapshot_not_ready", "请选择已完成或失败尝试中已持久化的记录")
    if snapshot.source_revision != source.revision:
        conflict("来源配置版本已改变，请重新采集样本")
    resource = next((r for r in source.resources if r["resource_id"] == mapping["resource_id"]), None)
    if not resource or not (
        schema_fingerprint(resource) == mapping["schema_fingerprint"] == snapshot.schema_fingerprint
    ):
        conflict("来源结构与映射或快照不一致，请修订映射并重新选择样本")
    if not resource.get("primary_key"):
        raise DomainError(422, "mapping_invalid", "映射需要显式稳定记录身份")
    source_fields = {field["name"] for field in resource["fields"]}
    properties = {prop["key"]: prop for prop in type_def["properties"]}
    for target, binding in mapping["fields"].items():
        if binding["source_field"] not in source_fields:
            schema_errors.append({"field": target, "message": "源字段不在已发现结构中"})
        if target not in properties or not properties[target].get("source_owned"):
            schema_errors.append({"field": target, "message": "映射目标必须为来源维护属性"})
    if schema_errors:
        raise DomainError(422, "mapping_invalid", "映射结构未通过校验", schema_errors)

    records = session.scalars(
        select(SourceRecordRevision).where(
            SourceRecordRevision.tenant_id == user.tenant_id,
            SourceRecordRevision.project_id == change.project_id,
            SourceRecordRevision.snapshot_id == snapshot.id,
            SourceRecordRevision.attempt == snapshot.attempt,
            SourceRecordRevision.id.in_(data.record_ids),
        )
    ).all()
    by_id = {record.id: record for record in records}
    if set(by_id) != set(data.record_ids):
        # Do not return a successful subset or identify which hidden ID exists.
        missing()
    budget = [0]
    rows = [preview_record(by_id[record_id], mapping, type_def, budget) for record_id in data.record_ids]
    capture_complete = snapshot.status == "complete"
    return {
        "binding": {
            "changeset_id": change.id,
            "changeset_revision": change.revision,
            "changeset_status": change.status,
            "baseline_release_id": change.baseline_release_id,
            "mapping_key": mapping["key"],
            "mapping_fingerprint": fingerprint(mapping),
            "snapshot_id": snapshot.id,
            "snapshot_attempt": snapshot.attempt,
            "snapshot_release_id": snapshot.release_id,
            "snapshot_status": snapshot.status,
            "consistency": snapshot.consistency,
            "source_id": source.id,
            "source_revision": source.revision,
            "resource_id": snapshot.resource_id,
            "schema_fingerprint": snapshot.schema_fingerprint,
        },
        "rows": rows,
        "summary": {
            "scope": "selected_records_only",
            "selected_count": len(rows),
            "valid_count": sum(row["valid"] for row in rows),
            "invalid_count": sum(not row["valid"] for row in rows),
            "capture_complete": capture_complete,
            "writes": 0,
            "message": "仅验证选中样本；不代表全量验证或对象投影结果。"
            if capture_complete
            else "仅诊断失败或部分采集的选中样本；不证明完整采集、全量验证或对象投影结果。",
        },
    }


@router.post("/mappings/preview")
def preview_mapping(data: MappingPreviewInput, request: Request, user=Depends(authenticated)):
    with request.app.state.db.transaction(user.tenant_id) as session:
        return preview(session, user, data)
