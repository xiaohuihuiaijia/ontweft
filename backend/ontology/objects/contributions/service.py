"""Atomic materialization from independent, immutable-evidenced contributions.

Callers hold the project write lock and control publication/stream fencing. No
function commits: source pointers, current ledger, history and entity roll back
together. A missing legacy ledger is never inferred from a new event.
"""

import json
from decimal import Decimal

from sqlalchemy import select

from ontology.cdc.models import CdcEvent, CdcStream
from ontology.cdc.values import CdcValueError, decode_row
from ontology.core.domain import validate_source_properties
from ontology.core.errors import DomainError, conflict, forbidden, missing
from ontology.modeling.models import Release
from ontology.objects.contributions.models import ContributionRevision, SourceContribution
from ontology.objects.models import Entity, EntityRevision
from ontology.sources.models import SourceIdentity, SourceRecordRevision, SourceSnapshot

VALUE_FIELDS = (
    "identity_id",
    "revision",
    "record_id",
    "record_codec",
    "release_id",
    "mapping_key",
    "mapped_values",
    "active",
    "withdraw_reason",
    "native_row",
    "stream_id",
    "sequence",
)


def _scoped(ctx, *rows):
    if "sources:write" not in ctx.permissions:
        forbidden()
    for row in rows:
        if row is None or row.tenant_id != ctx.user.tenant_id or row.project_id != ctx.project.id:
            missing()


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _same(prop, left, right):
    if left is None or right is None:
        return left is right
    if prop.get("data_type") == "number":
        return Decimal(str(left)) == Decimal(str(right))
    return _json(left) == _json(right)


def _revision(session, ctx, contribution):
    session.add(
        ContributionRevision(
            tenant_id=contribution.tenant_id,
            project_id=contribution.project_id,
            contribution_id=contribution.id,
            actor_id=ctx.user.id,
            **{field: getattr(contribution, field) for field in VALUE_FIELDS},
        )
    )


def _published_mapping(session, ctx, identity, release_id, mapping_key):
    release = session.get(Release, release_id)
    _scoped(ctx, identity, release)
    mappings = [
        item
        for item in release.definition.get("mappings", [])
        if (item["source_id"], item["resource_id"], item["type_key"], item["key"])
        == (identity.source_id, identity.resource_id, identity.type_key, mapping_key)
    ]
    types = [item for item in release.definition["types"] if item["key"] == identity.type_key]
    if len(mappings) != 1 or len(types) != 1:
        conflict("来源贡献缺少唯一已发布映射或类型，须审核血缘")
    return release, mappings[0], types[0]


def _current_rows(session, ctx, entity):
    return list(
        session.scalars(
            select(SourceContribution)
            .join(SourceIdentity, SourceIdentity.id == SourceContribution.identity_id)
            .where(
                SourceContribution.tenant_id == ctx.user.tenant_id,
                SourceContribution.project_id == ctx.project.id,
                SourceIdentity.entity_id == entity.id,
                SourceIdentity.tenant_id == ctx.user.tenant_id,
                SourceIdentity.project_id == ctx.project.id,
            )
            .order_by(SourceContribution.identity_id)
        )
    )


def ensure_legacy_contributions(session, ctx, entity) -> int:
    """Bootstrap only proved snapshot contributions, before changing identities.

    Existing object values are a consistency check, never an input from which a
    source contribution is guessed. A mismatch aborts the caller transaction.
    """
    from ontology.sources.ingestion import fingerprint, mapped_properties

    _scoped(ctx, entity)
    known = {row.identity_id for row in _current_rows(session, ctx, entity)}
    identities = list(
        session.scalars(
            select(SourceIdentity)
            .where(
                SourceIdentity.tenant_id == ctx.user.tenant_id,
                SourceIdentity.project_id == ctx.project.id,
                SourceIdentity.entity_id == entity.id,
            )
            .order_by(SourceIdentity.id)
        )
    )
    pending = []
    for identity in identities:
        if identity.id in known:
            continue
        record = session.get(SourceRecordRevision, identity.last_record_id)
        _scoped(ctx, record)
        if identity.last_record_codec != "snapshot-json-v1" or record.record_codec != "snapshot-json-v1":
            conflict("原生 CDC 身份缺少贡献账本，不能从引用或当前对象猜测历史贡献")
        snapshot = session.get(SourceSnapshot, record.snapshot_id)
        _scoped(ctx, snapshot)
        if (
            snapshot.source_id != identity.source_id
            or snapshot.resource_id != identity.resource_id
            or snapshot.status != "complete"
            or snapshot.attempt != record.attempt
            or record.key_hash != identity.key_hash
        ):
            conflict("旧身份与不可变快照证据不一致，须审核迁移")
        raw = {"key": record.source_key, "identity_key": record.identity_key, "values": record.values}
        if fingerprint(raw) != record.content_hash:
            conflict("旧来源原文摘要不匹配，不能重建贡献")
        release, mapping, type_def = _published_mapping(
            session, ctx, identity, snapshot.release_id, identity.mapping_key
        )
        if mapping["schema_fingerprint"] != snapshot.schema_fingerprint:
            conflict("旧快照结构与发布映射不一致，不能重建贡献")
        values = mapped_properties(record, mapping, type_def)
        props = {prop["key"]: prop for prop in type_def["properties"]}
        if any(
            field not in entity.properties or not _same(props[field], value, entity.properties[field])
            for field, value in values.items()
        ):
            conflict("旧来源贡献与对象值不一致，须人工审核；不能根据对象值补写原文")
        pending.append((identity, record, release, mapping, values))
    # Validate every missing contribution before adding any new ledger rows.
    for identity, record, release, mapping, values in pending:
        contribution = SourceContribution(
            tenant_id=ctx.user.tenant_id,
            project_id=ctx.project.id,
            identity_id=identity.id,
            revision=1,
            record_id=record.id,
            record_codec=record.record_codec,
            release_id=release.id,
            mapping_key=mapping["key"],
            mapped_values=values,
            active=True,
            withdraw_reason=None,
            native_row=None,
            stream_id=None,
            sequence=None,
        )
        session.add(contribution)
        session.flush()
        _revision(session, ctx, contribution)
    session.flush()
    return len(pending)


def _validate_record(session, ctx, identity, record, release_id, stream_id, sequence):
    _scoped(ctx, identity, record)
    if record.record_codec == "snapshot-json-v1":
        snapshot = session.get(SourceSnapshot, record.snapshot_id)
        _scoped(ctx, snapshot)
        if (
            snapshot.source_id != identity.source_id
            or snapshot.resource_id != identity.resource_id
            or snapshot.release_id != release_id
            or record.key_hash != identity.key_hash
            or snapshot.attempt != record.attempt
            or stream_id is not None
            or sequence is not None
        ):
            conflict("快照贡献的来源、身份或发布版不一致")
    elif record.record_codec == "cdc-native-reference-v1":
        event = session.get(CdcEvent, record.cdc_event_id)
        stream = session.get(CdcStream, stream_id) if stream_id else None
        _scoped(ctx, event, stream)
        if (
            event.stream_id != stream.id
            or event.sequence != sequence
            or stream.source_id != identity.source_id
        ):
            conflict("CDC 贡献的原始事件、序号或来源不一致")
    else:
        conflict("不支持的来源记录编码")


def replace_contribution(
    session,
    ctx,
    identity,
    record,
    release_id,
    mapping,
    type_def,
    mapped_values,
    *,
    native_row=None,
    stream_id=None,
    sequence=None,
    withdraw_reason=None,
    new_identity=False,
    new_entity=False,
):
    """Replace/withdraw one proved source contribution and recompute its entity.

    ``new_identity`` is only for an identity just created by a caller that first
    bootstrapped all prior identities of this entity. Null values are ordinary
    field values; withdrawal requires an explicit nonempty reason. ``new_entity``
    is reserved for a just-created, empty entity and saves its first revision as 1.
    """
    _scoped(ctx, identity, record)
    entity = session.get(Entity, identity.entity_id)
    _scoped(ctx, entity)
    if entity.deleted or entity.type_key != identity.type_key:
        conflict("来源身份指向已删除或不匹配的对象")
    if release_id != ctx.project.active_release_id:
        conflict("贡献投影的模型发布基线已改变")
    _, expected_mapping, expected_type = _published_mapping(
        session, ctx, identity, release_id, mapping["key"]
    )
    if mapping != expected_mapping or type_def != expected_type:
        conflict("贡献必须使用固定发布版中的完整映射和类型")
    _validate_record(session, ctx, identity, record, release_id, stream_id, sequence)
    if record.record_codec == "snapshot-json-v1" and native_row is not None:
        conflict("快照贡献不接受原生 CDC 行缓存")
    if native_row is not None:
        try:
            decode_row(native_row)
        except CdcValueError as exc:
            raise DomainError(
                422, "invalid_native_row", "原生行缓存必须为有效的 cdc-row-v1 类型编码"
            ) from exc
    if withdraw_reason is not None and (
        not isinstance(withdraw_reason, str) or not withdraw_reason.strip() or len(withdraw_reason) > 500
    ):
        raise DomainError(422, "invalid_withdrawal", "撤回必须提供有效原因")
    current_rows = _current_rows(session, ctx, entity)
    current = next((row for row in current_rows if row.identity_id == identity.id), None)
    if new_entity and (
        not new_identity
        or current_rows
        or entity.revision != 1
        or entity.properties
        or entity.evidence
        or session.scalar(select(EntityRevision.id).where(EntityRevision.entity_id == entity.id).limit(1))
    ):
        conflict("首次物化仅允许刚创建且尚无历史的空对象")
    if current is None and not new_identity:
        conflict("已有来源身份缺少贡献账本，请先从不可变旧证据完成迁移")
    if current is None:
        ledger_ids = {row.identity_id for row in current_rows}
        others = session.scalars(
            select(SourceIdentity.id).where(
                SourceIdentity.entity_id == entity.id,
                SourceIdentity.tenant_id == ctx.user.tenant_id,
                SourceIdentity.project_id == ctx.project.id,
                SourceIdentity.id != identity.id,
            )
        ).all()
        if any(other not in ledger_ids for other in others):
            conflict("同一对象仍有未审核的旧来源身份，请先迁移全部贡献")
    if (
        current
        and current.stream_id == stream_id
        and stream_id is not None
        and (sequence < current.sequence or (sequence == current.sequence and record.id != current.record_id))
    ):
        conflict("旧 CDC 事件不能覆盖较新的来源贡献")
    active = withdraw_reason is None
    if active:
        if not isinstance(mapped_values, dict) or set(mapped_values) != set(mapping["fields"]):
            raise DomainError(422, "incomplete_contribution", "贡献值必须覆盖所选映射的全部字段")
        values = validate_source_properties(type_def, mapped_values)
    else:
        if current is None or mapped_values is not None:
            conflict("撤回须使用已有贡献，不能把空值或新身份当作删除")
        values = dict(current.mapped_values)
        if native_row is None and record.record_codec == "cdc-native-reference-v1":
            native_row = current.native_row
    props = {prop["key"]: prop for prop in type_def["properties"]}
    if active and any(field not in props or not props[field].get("source_owned") for field in values):
        conflict("来源贡献只能管理已声明的来源维护属性")
    managed = {field for row in current_rows for field in row.mapped_values} | set(values)
    state = {
        "identity_id": identity.id,
        "record_id": record.id,
        "record_codec": record.record_codec,
        "release_id": release_id,
        "mapping_key": mapping["key"],
        "mapped_values": values,
        "active": active,
        "withdraw_reason": withdraw_reason,
        "native_row": native_row,
        "stream_id": stream_id,
        "sequence": sequence,
    }
    if current and all(_json(getattr(current, key)) == _json(value) for key, value in state.items()):
        return {"entity": entity, "changed": False, "contribution": current}
    if current:
        current.revision += 1
        for key, value in state.items():
            setattr(current, key, value)
    else:
        current = SourceContribution(
            tenant_id=ctx.user.tenant_id, project_id=ctx.project.id, revision=1, **state
        )
        session.add(current)
        current_rows.append(current)
    session.flush()
    _revision(session, ctx, current)
    identity.last_record_id, identity.last_record_codec = record.id, record.record_codec
    identity.mapping_key = mapping["key"]

    from ontology.objects.contributions.decisions import materialize_contributions

    changed = materialize_contributions(
        session,
        ctx,
        entity,
        type_def,
        rows=current_rows,
        managed_before=managed,
        new_entity=new_entity,
        reason="来源贡献撤回" if not active else "来源贡献物化",
    )
    return {"entity": entity, "changed": changed, "contribution": current}
