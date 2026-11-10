"""Durable snapshots and deterministic projection through a published mapping.

Raw pages are committed before projection. Restarting a transaction-bound source
starts a new attempt; incomplete attempts are retained and never infer deletions.
"""

import hashlib
import json
import time
from datetime import date
from decimal import Decimal, InvalidOperation

from sqlalchemy import select

from ontology.cdc.projection_models import CdcProjectionBinding
from ontology.core.domain import active_definition, validate_properties, validate_source_properties
from ontology.core.errors import DomainError, conflict
from ontology.core.models import now
from ontology.identity.models import User
from ontology.identity.security import access, audit
from ontology.modeling.models import Release
from ontology.jobs.service import check_job_authority, fenced_job, finish
from ontology.objects.contributions.service import ensure_legacy_contributions, replace_contribution
from ontology.objects.models import Entity
from ontology.sources.models import Source, SourceIdentity, SourceRecordRevision, SourceSnapshot


def fingerprint(value):
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    ).hexdigest()


def schema_fingerprint(resource):
    return fingerprint(
        {key: resource.get(key) for key in ("resource_id", "kind", "fields", "primary_key", "foreign_keys")}
    )


def mapping_errors(session, ctx, definition):
    errors, keys, writers = [], set(), set()
    types = {item["key"]: item for item in definition["types"]}
    for mapping in definition.get("mappings", []):
        key = mapping["key"]
        if key in keys:
            errors.append({"mapping": key, "message": "映射标识重复"})
        keys.add(key)
        source = session.scalar(
            select(Source).where(
                Source.id == mapping["source_id"],
                Source.tenant_id == ctx.user.tenant_id,
                Source.project_id == ctx.project.id,
            )
        )
        resource = (
            next((r for r in source.resources if r["resource_id"] == mapping["resource_id"]), None)
            if source
            else None
        )
        if not source or not resource:
            errors.append({"mapping": key, "message": "来源或已发现资源不存在"})
            continue
        if source.kind == "feishu":
            errors.append({"mapping": key, "message": "飞书对象投影须先配置当前员工授权服务"})
        if schema_fingerprint(resource) != mapping["schema_fingerprint"]:
            errors.append({"mapping": key, "message": "来源结构已改变，请修订映射后重新审核"})
        if not resource.get("primary_key"):
            errors.append({"mapping": key, "message": "映射需要显式稳定记录身份"})
        type_def = types.get(mapping["type_key"])
        if not type_def:
            errors.append({"mapping": key, "message": "目标对象类型未定义"})
            continue
        fields = {f["name"] for f in resource["fields"]}
        props = {p["key"]: p for p in type_def["properties"]}
        identity_fields = mapping.get("identity_fields", [])
        if identity_fields and (
            set(identity_fields) not in [set(k) for k in type_def.get("unique_keys", [])]
            or any(f not in mapping["fields"] for f in identity_fields)
        ):
            errors.append(
                {"mapping": key, "message": "跨来源身份绑定必须使用已定义唯一键，且映射覆盖全部键字段"}
            )
        writer = (mapping["source_id"], mapping["resource_id"], mapping["type_key"])
        if writer in writers:
            errors.append({"mapping": key, "message": "同一来源资源和类型只能有一个映射"})
        writers.add(writer)
        for target, binding in mapping["fields"].items():
            if binding["source_field"] not in fields:
                errors.append({"mapping": key, "field": target, "message": "源字段不存在"})
            if target not in props or not props[target].get("source_owned"):
                errors.append({"mapping": key, "field": target, "message": "映射目标必须声明为来源维护属性"})
        # Each source may contribute only some required fields. The combined
        # contribution ledger determines whether an entity is incomplete.
    for previous in active_definition(session, ctx.project).get("mappings", []):
        writer = (previous["source_id"], previous["resource_id"], previous["type_key"])
        if writer not in writers and session.scalar(
            select(SourceIdentity.id)
            .where(
                SourceIdentity.tenant_id == ctx.user.tenant_id,
                SourceIdentity.project_id == ctx.project.id,
                SourceIdentity.source_id == writer[0],
                SourceIdentity.resource_id == writer[1],
                SourceIdentity.type_key == writer[2],
            )
            .limit(1)
        ):
            errors.append(
                {
                    "mapping": previous["key"],
                    "message": "该映射已有对象贡献，须先完成来源撤回或迁移，不能直接移除",
                }
            )
    return errors


def transform_value(value, operation):
    if value is None or operation == "identity":
        return value
    if operation in {"trim", "lower", "upper"}:
        if not isinstance(value, str):
            raise ValueError("文本转换的输入必须是字符串")
        return value.strip() if operation == "trim" else getattr(value, operation)()
    if operation in {"number", "integer"}:
        if isinstance(value, bool) or not isinstance(value, (str, int, float)):
            raise ValueError("数值转换的输入类型不合法")
        decimal = Decimal(str(value))
        if not decimal.is_finite() or (operation == "integer" and decimal != decimal.to_integral_value()):
            raise ValueError("数值不是有效有限值或整数")
        # Enforce the existing object integer budget before expanding a short
        # exponent-form source value into a potentially enormous Python int.
        if operation == "integer" and not decimal.is_zero() and decimal.adjusted() >= 1000:
            raise ValueError("整数长度不得超过 1000 位")
        return int(decimal) if operation == "integer" else str(decimal)
    if operation == "boolean":
        if isinstance(value, bool):
            return value
        if isinstance(value, str) and value.lower() in {"true", "false"}:
            return value.lower() == "true"
        raise ValueError("布尔转换仅接受 true/false")
    if operation == "date" and isinstance(value, str):
        return date.fromisoformat(value).isoformat()
    raise ValueError("不支持的转换或输入类型")


def mapped_properties(record, mapping, type_def, *, allow_incomplete=False):
    result = {}
    for target, binding in mapping["fields"].items():
        field = binding["source_field"]
        if field not in record.values:
            raise DomainError(
                422,
                "missing_source_field",
                "源记录缺少映射字段；缺失值不能当作 null",
                {"record_id": record.id, "field": field},
            )
        try:
            result[target] = transform_value(record.values[field], binding["transform"])
        except (ValueError, TypeError, InvalidOperation, OverflowError) as exc:
            raise DomainError(
                422,
                "mapping_conversion_failed",
                "来源转换失败，请检查记录与映射",
                {"record_id": record.id, "field": target},
            ) from exc
    return (
        validate_source_properties(type_def, result)
        if allow_incomplete
        else validate_properties(type_def, result, partial=True)
    )


def resolve_identity(session, job, snapshot, mapping, record, *, candidates=None):
    scope = (
        SourceIdentity.source_id == snapshot.source_id,
        SourceIdentity.resource_id == snapshot.resource_id,
        SourceIdentity.type_key == mapping["type_key"],
        SourceIdentity.tenant_id == job.tenant_id,
    )
    codec = "typed-v1" if record.identity_key is not None else "legacy-json-v1"
    if candidates is None:
        candidates = session.scalars(
            select(SourceIdentity).where(
                *scope, SourceIdentity.key_hash.in_({record.key_hash, fingerprint(record.source_key)})
            )
        ).all()
    identity = next((row for row in candidates if row.key_hash == record.key_hash), None)
    if identity and identity.key_codec != codec:
        conflict("来源身份编码冲突，需要审核身份迁移")
    if codec == "typed-v1":
        legacy_hash = fingerprint(record.source_key)
        legacy = next(
            (row for row in candidates if row.key_codec == "legacy-json-v1" and row.key_hash == legacy_hash),
            None,
        )
        if legacy:
            if identity and identity.id != legacy.id:
                conflict("规范身份与旧身份指向不同对象，需要人工核对")
            previous = session.get(SourceRecordRevision, legacy.last_record_id)
            previous_snapshot = session.get(SourceSnapshot, previous.snapshot_id) if previous else None
            if (
                previous_snapshot is None
                or previous_snapshot.schema_fingerprint != snapshot.schema_fingerprint
            ):
                conflict(
                    "旧身份的来源结构发生变化，须审核身份迁移，不能根据已丢失原生类型的 JSON 主键自动合并"
                )
            # MongoDB document-valued IDs distinguish nested field order;
            # Python dict equality and sort_keys hashing do not. An upgrade
            # must preserve order and JSON scalar types, not just compare ==.
            if previous is None or json.dumps(
                previous.source_key, ensure_ascii=False, separators=(",", ":"), allow_nan=False
            ) != json.dumps(record.source_key, ensure_ascii=False, separators=(",", ":"), allow_nan=False):
                conflict("旧来源身份缺少匹配的原始证据，需要审核迁移")
            # Only an exact raw-key match can upgrade a legacy identity. Old raw
            # evidence stays immutable; changed scale/type is never guessed.
            legacy.key_hash, legacy.key_codec = record.key_hash, codec
            identity = legacy
    return identity, codec


def bound_entity(session, ctx, mapping, type_def, properties):
    fields = mapping.get("identity_fields", [])
    if not fields:
        return None
    from ontology.objects.constraints import key_values
    from ontology.objects.models import EntityKey

    key = key_values(type_def, properties, fields)
    if key is None:
        conflict("身份绑定的业务键不能为空")
    entity_id = session.scalar(
        select(EntityKey.entity_id).where(
            EntityKey.tenant_id == ctx.user.tenant_id,
            EntityKey.project_id == ctx.project.id,
            EntityKey.definition_id == ctx.project.active_release_id,
            EntityKey.type_key == mapping["type_key"],
            EntityKey.fields_hash == key[0],
            EntityKey.value_hash == key[1],
        )
    )
    entity = session.get(Entity, entity_id) if entity_id else None
    if entity:
        ensure_legacy_contributions(session, ctx, entity)
    return entity


def project_snapshot(session, job, snapshot):
    user = session.get(User, job.owner_id)
    ctx = access(session, user, job.project_id, "sources:write")
    source = session.get(Source, snapshot.source_id)
    if source.revision != snapshot.source_revision:
        conflict("来源配置版本已改变，保留原始记录并重新采集")
    if ctx.project.active_release_id != snapshot.release_id:
        conflict("本体发布基线已改变，保留原始记录并重新采集或重放")
    bindings = session.scalars(
        select(CdcProjectionBinding).where(
            CdcProjectionBinding.tenant_id == job.tenant_id,
            CdcProjectionBinding.project_id == job.project_id,
            CdcProjectionBinding.source_id == snapshot.source_id,
        )
    )
    if any(
        any(resource["resource_id"] == snapshot.resource_id for resource in binding.resources)
        for binding in bindings
    ):
        conflict("资源已绑定 CDC 业务投影；普通快照保留原文但不能覆写，请使用 CDC 原生完整快照或审核重建基线")
    newer = session.scalar(
        select(SourceSnapshot.id)
        .where(
            SourceSnapshot.source_id == snapshot.source_id,
            SourceSnapshot.resource_id == snapshot.resource_id,
            SourceSnapshot.generation > snapshot.generation,
        )
        .limit(1)
    )
    if newer:
        conflict("已有更新的采集请求，旧快照保留原始记录但不能覆盖对象")
    release = session.get(Release, snapshot.release_id)
    definition = release.definition
    errors = mapping_errors(session, ctx, definition)
    if errors:
        raise DomainError(422, "mapping_invalid", "映射校验未通过", errors)
    mappings = [
        m
        for m in definition.get("mappings", [])
        if m["source_id"] == snapshot.source_id and m["resource_id"] == snapshot.resource_id
    ]
    types = {t["key"]: t for t in definition["types"]}
    counts = {"created": 0, "updated": 0, "unchanged": 0, "mappings": len(mappings), "deleted": 0}
    for mapping in mappings:
        codecs = set()
        records = session.scalars(
            select(SourceRecordRevision)
            .where(
                SourceRecordRevision.snapshot_id == snapshot.id,
                SourceRecordRevision.attempt == snapshot.attempt,
            )
            .order_by(SourceRecordRevision.id)
            .execution_options(yield_per=500)
        )
        for record in records:
            try:
                with session.begin_nested():
                    # Bootstrap before resolve_identity can upgrade a legacy key hash.
                    # Only immutable evidence proves its old contribution; object values
                    # are a consistency check, never the reconstruction source.
                    candidates = session.scalars(
                        select(SourceIdentity).where(
                            SourceIdentity.tenant_id == job.tenant_id,
                            SourceIdentity.project_id == job.project_id,
                            SourceIdentity.source_id == snapshot.source_id,
                            SourceIdentity.resource_id == snapshot.resource_id,
                            SourceIdentity.type_key == mapping["type_key"],
                            SourceIdentity.key_hash.in_({record.key_hash, fingerprint(record.source_key)}),
                        )
                    ).all()
                    for candidate in candidates:
                        ensure_legacy_contributions(session, ctx, session.get(Entity, candidate.entity_id))
                    identity, codec = resolve_identity(
                        session, job, snapshot, mapping, record, candidates=candidates
                    )
                    codecs.add(codec)
                    if len(codecs) > 1:
                        conflict("同一资源的来源身份编码不一致")
                    obj = session.get(Entity, identity.entity_id) if identity else None
                    if obj and obj.deleted:
                        conflict("映射对象已被人工删除，须先处理身份冲突")
                    properties = mapped_properties(
                        record, mapping, types[mapping["type_key"]], allow_incomplete=True
                    )
                    new_identity = identity is None
                    if new_identity:
                        obj = bound_entity(session, ctx, mapping, types[mapping["type_key"]], properties)
                    created = obj is None
                    if created:
                        obj = Entity(
                            tenant_id=job.tenant_id,
                            project_id=job.project_id,
                            type_key=mapping["type_key"],
                            properties={},
                            evidence=[],
                            release_id=snapshot.release_id,
                        )
                        session.add(obj)
                        session.flush()
                    if new_identity:
                        identity = SourceIdentity(
                            tenant_id=job.tenant_id,
                            project_id=job.project_id,
                            source_id=snapshot.source_id,
                            resource_id=snapshot.resource_id,
                            key_hash=record.key_hash,
                            key_codec=codec,
                            type_key=mapping["type_key"],
                            entity_id=obj.id,
                            last_record_id=record.id,
                            last_record_codec=record.record_codec,
                            mapping_key=mapping["key"],
                        )
                        session.add(identity)
                        session.flush()
                    result = replace_contribution(
                        session,
                        ctx,
                        identity,
                        record,
                        snapshot.release_id,
                        mapping,
                        types[mapping["type_key"]],
                        properties,
                        new_identity=new_identity,
                        new_entity=created,
                    )
                    session.flush()
                    outcome = "created" if created else "updated" if result["changed"] else "unchanged"
                counts[outcome] += 1
            except DomainError as exc:
                if exc.status not in {409, 422}:
                    raise
                counts["blocked"] = counts.get("blocked", 0) + 1
                issues = counts.setdefault("issues", [])
                if len(issues) < 100:
                    issues.append({"record_id": record.id, "code": exc.code, "message": exc.message})
        source = session.get(Source, snapshot.source_id)
        if source.kind == "tabular" and source.config.get("file_mode") == "replace":
            if not job.checkpoint.get("complete") or snapshot.consistency != "transaction":
                conflict("完整替换需要采集完整且一致的文件快照")
            from ontology.objects.contributions.models import SourceContribution

            present = select(SourceRecordRevision.key_hash).where(
                SourceRecordRevision.snapshot_id == snapshot.id,
                SourceRecordRevision.attempt == snapshot.attempt,
            )
            missing_identities = session.scalars(
                select(SourceIdentity).where(
                    SourceIdentity.tenant_id == job.tenant_id,
                    SourceIdentity.source_id == source.id,
                    SourceIdentity.resource_id == snapshot.resource_id,
                    SourceIdentity.type_key == mapping["type_key"],
                    SourceIdentity.key_hash.not_in(present),
                )
            ).all()
            for identity in missing_identities:
                contribution = session.scalar(
                    select(SourceContribution).where(SourceContribution.identity_id == identity.id)
                )
                if not contribution or not contribution.active:
                    continue
                record = session.get(SourceRecordRevision, identity.last_record_id)
                replace_contribution(
                    session,
                    ctx,
                    identity,
                    record,
                    snapshot.release_id,
                    mapping,
                    types[mapping["type_key"]],
                    None,
                    withdraw_reason=f"complete_file_replacement:{snapshot.id}",
                    withdrawal_snapshot=snapshot,
                )
                counts["withdrawn"] = counts.get("withdrawn", 0) + 1
        if codecs:
            session.flush()
            remaining = session.scalar(
                select(SourceIdentity.id)
                .where(
                    SourceIdentity.source_id == snapshot.source_id,
                    SourceIdentity.resource_id == snapshot.resource_id,
                    SourceIdentity.type_key == mapping["type_key"],
                    SourceIdentity.key_codec != next(iter(codecs)),
                )
                .limit(1)
            )
            if remaining:
                conflict(
                    "存在无法精确匹配的旧来源身份，原始记录已保留；须审核身份迁移后再投影，不能自动新建重复对象"
                )
    return counts


def run_snapshot(db, settings, run):
    from ontology.sources.service import connector_for

    connector = None
    try:
        with db.transaction(run["tenant_id"]) as session:
            job = fenced_job(session, run)
            check_job_authority(session, job)
            snapshot = session.get(SourceSnapshot, job.payload["snapshot_id"])
            source = session.get(Source, snapshot.source_id)
            if source.revision != snapshot.source_revision:
                conflict("来源配置版本已改变")
            connector = connector_for(source, settings)
            resource_id, expected_schema = snapshot.resource_id, snapshot.schema_fingerprint
            snapshot.attempt = run["fencing_token"]
            snapshot.status, snapshot.started_at, snapshot.record_count, snapshot.error = (
                "capturing",
                now(),
                0,
                None,
            )
            job.checkpoint = {
                "attempt": snapshot.attempt,
                "records": 0,
                "restart_policy": "new_snapshot_attempt",
            }
        resources = [r.model_dump(mode="json") for r in connector.discover()]
        resource = next((r for r in resources if r["resource_id"] == resource_id), None)
        if not resource or schema_fingerprint(resource) != expected_schema:
            raise DomainError(409, "source_schema_changed", "源结构已变化，旧目录和正式映射保持不变")
        cursor, count, snapshot_token = None, 0, None
        while True:
            page = connector.read_snapshot(resource_id, cursor=cursor, limit=1000)
            if snapshot_token and page.snapshot_id != snapshot_token:
                raise DomainError(409, "snapshot_changed", "连接器快照身份发生变化")
            snapshot_token = page.snapshot_id
            with db.transaction(run["tenant_id"]) as session:
                job = fenced_job(session, run)
                check_job_authority(session, job)
                snapshot = session.get(SourceSnapshot, job.payload["snapshot_id"])
                source = session.get(Source, snapshot.source_id)
                if source.revision != snapshot.source_revision:
                    conflict("来源配置已改变")
                for item in page.records:
                    record = item.model_dump(mode="json")
                    if not record["key"] or any(v is None for v in record["key"].values()):
                        raise DomainError(422, "unstable_source_key", "记录缺少稳定来源身份")
                    identity_key = record.get("identity_key")
                    if identity_key is not None and set(identity_key) != set(record["key"]):
                        raise DomainError(422, "invalid_identity_key", "规范身份必须覆盖完整来源主键")
                    session.add(
                        SourceRecordRevision(
                            tenant_id=job.tenant_id,
                            project_id=job.project_id,
                            snapshot_id=snapshot.id,
                            attempt=snapshot.attempt,
                            source_key=record["key"],
                            identity_key=identity_key,
                            key_hash=fingerprint({"codec": "typed-v1", "key": identity_key})
                            if identity_key is not None
                            else fingerprint(record["key"]),
                            values=record["values"],
                            content_hash=fingerprint(record),
                        )
                    )
                count += len(page.records)
                if count > 1_000_000:
                    raise DomainError(422, "snapshot_budget", "单次采集超出当前配置预算，原始分页已保留")
                snapshot.record_count, snapshot.consistency = count, page.consistency
                job.checkpoint = {
                    "attempt": snapshot.attempt,
                    "records": count,
                    "snapshot_token": snapshot_token,
                    "complete": page.complete,
                    "restart_policy": "new_snapshot_attempt",
                }
                job.lease_until = time.time() + 300
            if page.complete:
                break
            if not page.next_cursor or page.next_cursor == cursor:
                raise DomainError(409, "invalid_source_cursor", "连接器未提供可继续的分页位置")
            cursor = page.next_cursor

        def apply(session, job):
            snapshot = session.get(SourceSnapshot, job.payload["snapshot_id"])
            snapshot.status = "complete"
            snapshot.projection = project_snapshot(session, job, snapshot)
            snapshot.status, snapshot.completed_at = "complete", now()
            user = session.get(User, job.owner_id)
            audit(
                session,
                user,
                job.project_id,
                "source.snapshot.completed",
                snapshot.id,
                {"records": count, "projection": snapshot.projection},
            )

        finish(db, run, result={"records": count, "snapshot_id": run["payload"]["snapshot_id"]}, apply=apply)
    finally:
        if connector:
            connector.close()
