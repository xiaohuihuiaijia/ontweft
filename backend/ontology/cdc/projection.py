"""Project one complete native source window atomically with its durable cursor.

The inbox is immutable and is never decoded into legacy snapshot JSON columns.
Source discovery checks the catalog before every run; it never fetches current
row values to fill gaps in historical events. Committed windows are also the
durable input for future index work, whose separate cursor is not advanced here.
"""

import json
from dataclasses import asdict
from types import SimpleNamespace

from sqlalchemy import or_, select

from ontology.cdc.decode import CdcDecodeError, decode_pg_event
from ontology.cdc.models import CdcEvent, CdcStream
from ontology.cdc.projection_models import CdcProjectionBinding, CdcProjectionState, CdcProjectionWindow
from ontology.cdc.values import CdcValueError, RowBinding, decode_row, encode_row, mapping_values, merge_row
from ontology.cdc.windows import next_cdc_window
from ontology.cdc.windows import schema_fingerprint as native_fingerprint
from ontology.core.domain import active_definition
from ontology.core.errors import DomainError, conflict
from ontology.core.models import uid
from ontology.identity.models import Project, User
from ontology.identity.security import access, audit
from ontology.jobs.models import Job
from ontology.jobs.service import check_job_authority, fenced_job, finish
from ontology.modeling.diff import normalized_mapping
from ontology.modeling.models import Release
from ontology.objects.contributions.models import SourceContribution
from ontology.objects.contributions.service import ensure_legacy_contributions, replace_contribution
from ontology.objects.models import Entity
from ontology.sources.ingestion import (
    bound_entity,
    fingerprint,
    mapped_properties,
    mapping_errors,
    schema_fingerprint,
)
from ontology.sources.models import Source, SourceIdentity, SourceRecordRevision
from ontology.sources.service import connector_for

MAX_EVENTS = 10_000
MAX_BYTES = 64 * 1024 * 1024


def binding_matches_model(session, project, binding):
    if project.active_release_id == binding.release_id:
        return True
    old = session.get(Release, binding.release_id)
    current = active_definition(session, project)

    def contract(definition):
        mappings = sorted(
            (
                normalized_mapping(m)
                for m in definition.get("mappings", [])
                if m["source_id"] == binding.source_id
            ),
            key=lambda m: m["key"],
        )
        keys = {m["type_key"] for m in mappings}
        types = []
        for definition_type in definition["types"]:
            if definition_type["key"] not in keys:
                continue
            # Presentation-only renames do not invalidate source decoding.
            semantic = {
                key: value
                for key, value in definition_type.items()
                if key not in {"label", "description", "aliases"}
            }
            semantic.setdefault("key_semantics", "legacy")
            semantic.setdefault("unique_keys", [])
            for section in ("properties", "relations"):
                semantic[section] = sorted(
                    (
                        {key: value for key, value in item.items() if key != "label"}
                        for item in definition_type.get(section, [])
                    ),
                    key=lambda item: item["key"],
                )

            def field_contract(field):
                return {
                    "required": False,
                    "enum": [],
                    "unit": None,
                    "read_roles": [],
                    "write_roles": [],
                    "source_owned": False,
                    **{key: value for key, value in field.items() if key != "label"},
                }

            semantic["properties"] = [field_contract(field) for field in semantic["properties"]]
            for relation in semantic["relations"]:
                relation.setdefault("cardinality", "many")
                relation["properties"] = sorted(
                    (field_contract(field) for field in relation.get("properties", [])),
                    key=lambda field: field["key"],
                )
            types.append(semantic)
        return mappings, sorted(types, key=lambda t: t["key"])

    return old is not None and contract(old.definition) == contract(current)


def enqueue_projection(session, stream, state, owner_id):
    previous = session.get(Job, state.last_job_id) if state.last_job_id else None
    if previous and previous.status in {"pending", "running"}:
        return previous
    job = Job(
        tenant_id=stream.tenant_id,
        project_id=stream.project_id,
        kind="cdc.project",
        owner_id=owner_id,
        payload={"stream_id": stream.id, "binding_id": state.binding_id},
    )
    session.add(job)
    session.flush()
    state.last_job_id = job.id
    return job


def schedule_pending(db, tenant_id):
    """Reuse ordinary leased jobs; new arrivals wake only non-blocked streams."""
    with db.transaction(tenant_id) as session:
        candidates = session.execute(
            select(CdcProjectionState.stream_id, CdcProjectionState.project_id)
            .join(
                CdcStream,
                CdcStream.id == CdcProjectionState.stream_id,
            )
            .where(
                CdcProjectionState.tenant_id == tenant_id,
                CdcProjectionState.status.in_(["waiting", "caught_up"]),
                CdcStream.state == "active",
                CdcStream.received_sequence > CdcStream.projected_sequence,
                or_(
                    CdcStream.received_sequence > CdcProjectionState.observed_sequence,
                    CdcProjectionState.reason_code == "has_more",
                ),
            )
            .order_by(CdcProjectionState.created_at)
            .limit(100)
        ).all()
    queued = False
    for stream_id, project_id in candidates:
        with db.transaction(tenant_id) as session:
            session.scalar(
                select(Project)
                .where(Project.id == project_id, Project.tenant_id == tenant_id)
                .with_for_update()
            )
            stream = session.scalar(select(CdcStream).where(CdcStream.id == stream_id).with_for_update())
            state = session.scalar(
                select(CdcProjectionState).where(CdcProjectionState.stream_id == stream_id).with_for_update()
            )
            if (
                not stream
                or not state
                or stream.state != "active"
                or state.status not in {"waiting", "caught_up"}
            ):
                continue
            if stream.received_sequence <= stream.projected_sequence:
                continue
            if stream.received_sequence <= state.observed_sequence and state.reason_code != "has_more":
                continue
            binding = session.get(CdcProjectionBinding, state.binding_id)
            job = enqueue_projection(session, stream, state, binding.owner_id)
            queued = queued or job.status == "pending"
    return queued


def context_for_run(session, run):
    job = fenced_job(session, run)
    check_job_authority(session, job)
    user = session.get(User, job.owner_id)
    ctx = access(session, user, job.project_id, "sources:write")
    session.refresh(ctx.project)
    stream = session.scalar(
        select(CdcStream)
        .where(
            CdcStream.id == job.payload["stream_id"],
            CdcStream.tenant_id == job.tenant_id,
            CdcStream.project_id == job.project_id,
        )
        .with_for_update()
    )
    state = (
        session.scalar(
            select(CdcProjectionState).where(CdcProjectionState.stream_id == stream.id).with_for_update()
        )
        if stream
        else None
    )
    binding = session.get(CdcProjectionBinding, job.payload["binding_id"])
    if (
        not stream
        or not state
        or not binding
        or state.binding_id != binding.id
        or state.last_job_id != job.id
    ):
        conflict("任务的投影配置已失效")
    source = session.get(Source, binding.source_id)
    if (
        not source
        or source.tenant_id != job.tenant_id
        or source.project_id != job.project_id
        or binding.tenant_id != job.tenant_id
        or binding.project_id != job.project_id
        or binding.stream_id != stream.id
        or source.id != stream.source_id
        or source.kind != "postgres"
        or source.revision != binding.source_revision
        or stream.source_revision != binding.source_revision
        or stream.generation != binding.generation
        or not binding_matches_model(session, ctx.project, binding)
    ):
        raise DomainError(409, "binding_changed", "模型或来源配置已改变，需要审核迁移和重建基线")
    if stream.state != "active":
        raise DomainError(409, "stream_paused", "采集通道已暂停")
    # The configuration owner's current authorization remains relevant after a
    # second user queues a run; user revocation cannot be bypassed by a job.
    owner = session.scalar(
        select(User)
        .where(User.id == binding.owner_id, User.tenant_id == job.tenant_id)
        .with_for_update(key_share=True)
        .execution_options(populate_existing=True)
    )
    if not owner or not owner.active:
        raise DomainError(403, "projection_authorization_revoked", "投影配置负责人的权限已失效")
    access(session, owner, job.project_id, "sources:write")
    return ctx, job, stream, state, binding, source


def reference_record(session, ctx, event):
    record = session.scalar(select(SourceRecordRevision).where(SourceRecordRevision.cdc_event_id == event.id))
    if record:
        return record
    record = SourceRecordRevision(
        tenant_id=ctx.user.tenant_id,
        project_id=ctx.project.id,
        record_codec="cdc-native-reference-v1",
        cdc_event_id=event.id,
    )
    session.add(record)
    session.flush()
    return record


def identity_for_change(session, ctx, stream, mapping, change):
    scope = [
        SourceIdentity.tenant_id == ctx.user.tenant_id,
        SourceIdentity.project_id == ctx.project.id,
        SourceIdentity.source_id == stream.source_id,
        SourceIdentity.resource_id == change.resource_id,
        SourceIdentity.type_key == mapping["type_key"],
    ]
    key_hash = fingerprint({"codec": "typed-v1", "key": change.identity_key})
    identity = session.scalar(select(SourceIdentity).where(*scope, SourceIdentity.key_hash == key_hash))
    if identity and identity.key_codec != "typed-v1":
        conflict("规范来源身份存在编码冲突")
    # Legacy JSON can lose numeric, binary and temporal key distinctions. Never
    # create a second object while an unreviewed legacy identity might match.
    if session.scalar(
        select(SourceIdentity.id).where(*scope, SourceIdentity.key_codec != "typed-v1").limit(1)
    ):
        raise DomainError(
            409, "legacy_identity_review_required", "该资源存在旧 JSON 身份，请先审核无损身份迁移"
        )
    return identity, key_hash


def previous_row(session, contribution, binding, change):
    if not contribution or not contribution.active or contribution.native_row is None:
        return None, None
    if contribution.stream_id != binding.stream_id or contribution.record_codec != "cdc-native-reference-v1":
        conflict("历史行的采集通道不匹配")
    record = session.get(SourceRecordRevision, contribution.record_id)
    event = session.get(CdcEvent, record.cdc_event_id)
    previous = decode_pg_event(event.record)
    if previous.kind != "data" or previous.operation not in {"r", "c", "u"}:
        conflict("历史原生行缺少可验证的数据事件")
    prior_binding = RowBinding(
        previous.resource_id, native_fingerprint(event.record), previous.identity_key, binding.generation
    )
    return decode_row(contribution.native_row), prior_binding


def apply_data(session, ctx, stream, binding, event, change, mapping, type_def, resource):
    if set(change.identity_key or {}) != set(resource["primary_key"]):
        raise DomainError(409, "source_identity_changed", "原生事件主键与审核目录不一致")
    identity, key_hash = identity_for_change(session, ctx, stream, mapping, change)
    new_identity = identity is None
    entity = session.get(Entity, identity.entity_id) if identity else None
    if entity:
        ensure_legacy_contributions(session, ctx, entity)
    contribution = (
        session.scalar(select(SourceContribution).where(SourceContribution.identity_id == identity.id))
        if identity
        else None
    )
    if change.operation in {"u", "d"} and (not contribution or not contribution.active):
        raise DomainError(409, "source_identity_missing", "更新或删除缺少当前有效来源贡献，须审核完整性")
    if change.operation == "c" and contribution and contribution.active:
        raise DomainError(409, "source_identity_exists", "新增事件与已有活动身份冲突")
    record = reference_record(session, ctx, event)
    if change.operation == "d":
        result = replace_contribution(
            session,
            ctx,
            identity,
            record,
            ctx.project.active_release_id,
            mapping,
            type_def,
            None,
            stream_id=stream.id,
            sequence=event.sequence,
            withdraw_reason="native_delete",
        )
        return result, False, True
    if change.after is None or set(change.after.fields) != {field["name"] for field in resource["fields"]}:
        raise DomainError(409, "source_schema_changed", "原生行字段与审核目录不一致")
    previous, prior_binding = (
        previous_row(session, contribution, binding, change) if change.operation == "u" else (None, None)
    )
    row_binding = RowBinding(
        change.resource_id, native_fingerprint(event.record), change.identity_key, binding.generation
    )
    merged = merge_row(
        change.after,
        operation=change.operation,
        binding=row_binding,
        previous=previous,
        previous_binding=prior_binding,
    )
    values = mapping_values(
        merged, fields=list(dict.fromkeys(field["source_field"] for field in mapping["fields"].values()))
    )
    mapped = mapped_properties(
        SimpleNamespace(id=record.id, values=values), mapping, type_def, allow_incomplete=True
    )
    if new_identity:
        entity = bound_entity(session, ctx, mapping, type_def, mapped)
    new_entity = entity is None
    if new_entity:
        entity = Entity(
            id=uid(),
            tenant_id=ctx.user.tenant_id,
            project_id=ctx.project.id,
            type_key=mapping["type_key"],
            properties={},
            evidence=[],
            release_id=ctx.project.active_release_id,
        )
        session.add(entity)
        session.flush()
    if new_identity:
        identity = SourceIdentity(
            tenant_id=ctx.user.tenant_id,
            project_id=ctx.project.id,
            source_id=stream.source_id,
            resource_id=change.resource_id,
            key_hash=key_hash,
            key_codec="typed-v1",
            type_key=mapping["type_key"],
            entity_id=entity.id,
            mapping_key=mapping["key"],
            last_record_id=record.id,
            last_record_codec=record.record_codec,
        )
        session.add(identity)
        session.flush()
    result = replace_contribution(
        session,
        ctx,
        identity,
        record,
        ctx.project.active_release_id,
        mapping,
        type_def,
        mapped,
        native_row=encode_row(merged),
        stream_id=stream.id,
        sequence=event.sequence,
        new_identity=new_identity,
        new_entity=new_entity,
    )
    return result, new_identity, False


def project_window(session, ctx, stream, state, binding, unit, events):
    if unit.kind == "transaction":
        baseline = (
            session.get(CdcProjectionWindow, state.baseline_window_id) if state.baseline_window_id else None
        )
        if (
            not baseline
            or baseline.kind != "snapshot"
            or baseline.stream_id != stream.id
            or baseline.binding_id != binding.id
            or baseline.tenant_id != ctx.user.tenant_id
            or baseline.project_id != ctx.project.id
            or baseline.end_sequence > stream.projected_sequence
            or not baseline.proof
            or baseline.proof.get("kind") not in {"completed_notification", "resumed_completed_offset"}
            or set(baseline.proof.get("resources", [])) != {r["resource_id"] for r in binding.resources}
        ):
            raise DomainError(409, "baseline_required", "必须先完成带结束证明的原生初始快照")
    definition = active_definition(session, ctx.project)
    errors = mapping_errors(session, ctx, definition, source_id=binding.source_id)
    if errors:
        raise DomainError(409, "mapping_invalid", "投影映射已失效，需要审核", errors)
    resources = {r["resource_id"]: r for r in binding.resources}
    mappings = [m for m in definition.get("mappings", []) if m["source_id"] == stream.source_id]
    types = {t["key"]: t for t in definition["types"]}
    for resource, schema in unit.schema_fingerprints.items():
        if resource not in resources:
            raise DomainError(409, "capture_scope_changed", "事件超出已审核采集范围")
        if resource in state.schema_fingerprints and state.schema_fingerprints[resource] != schema:
            raise DomainError(409, "native_schema_changed", "原生结构发生变化，需要审核映射及重建基线")
    event_by_id = {event.event_id: event for event in events}
    counts = {"created": 0, "updated": 0, "unchanged": 0, "withdrawn": 0}
    touched, seen = set(), set()
    for change in unit.changes:
        if change.kind == "tombstone":
            continue
        event = event_by_id[change.event_id]
        for mapping in mappings:
            if mapping["resource_id"] != change.resource_id:
                continue
            result, created, withdrawn = apply_data(
                session,
                ctx,
                stream,
                binding,
                event,
                change,
                mapping,
                types[mapping["type_key"]],
                resources[change.resource_id],
            )
            seen.add(result["contribution"].identity_id)
            touched.add(result["entity"].id)
            counts[
                "created"
                if created
                else "withdrawn"
                if withdrawn
                else "updated"
                if result["changed"]
                else "unchanged"
            ] += 1
    if unit.kind == "snapshot":
        if not unit.snapshot_proof or set(unit.snapshot_proof.resources) != set(resources):
            raise DomainError(409, "baseline_proof_missing", "完整快照缺少准确范围的结束证明")
        completion = next(event for event in events if event.sequence == unit.end_sequence)
        for mapping in mappings:
            identities = list(
                session.scalars(
                    select(SourceIdentity).where(
                        SourceIdentity.tenant_id == ctx.user.tenant_id,
                        SourceIdentity.project_id == ctx.project.id,
                        SourceIdentity.source_id == stream.source_id,
                        SourceIdentity.resource_id == mapping["resource_id"],
                        SourceIdentity.type_key == mapping["type_key"],
                    )
                )
            )
            for identity in identities:
                if identity.id in seen:
                    continue
                entity = session.get(Entity, identity.entity_id)
                ensure_legacy_contributions(session, ctx, entity)
                contribution = session.scalar(
                    select(SourceContribution).where(SourceContribution.identity_id == identity.id)
                )
                if contribution and contribution.active:
                    result = replace_contribution(
                        session,
                        ctx,
                        identity,
                        reference_record(session, ctx, completion),
                        ctx.project.active_release_id,
                        mapping,
                        types[mapping["type_key"]],
                        None,
                        stream_id=stream.id,
                        sequence=completion.sequence,
                        withdraw_reason="complete_baseline_absent",
                    )
                    touched.add(result["entity"].id)
                    counts["withdrawn"] += 1
    last_change = decode_pg_event(unit.records[-1][1])
    window = CdcProjectionWindow(
        tenant_id=ctx.user.tenant_id,
        project_id=ctx.project.id,
        binding_id=binding.id,
        stream_id=stream.id,
        start_sequence=unit.start_sequence,
        end_sequence=unit.end_sequence,
        kind=unit.kind,
        proof=asdict(unit.snapshot_proof) if unit.snapshot_proof else None,
        schema_fingerprints=unit.schema_fingerprints,
        result={**counts, "release_id": ctx.project.active_release_id},
        entity_ids=sorted(touched),
        source_position=last_change.source_position,
    )
    session.add(window)
    session.flush()
    if unit.kind == "snapshot":
        state.baseline_window_id = window.id
    state.schema_fingerprints = {**state.schema_fingerprints, **unit.schema_fingerprints}
    stream.projected_sequence = unit.end_sequence
    state.status = "caught_up" if stream.projected_sequence == stream.received_sequence else "waiting"
    state.reason_code = "caught_up" if state.status == "caught_up" else "has_more"
    state.message = "完整窗口已提交到本体对象，索引水位单独管理"
    audit(
        session,
        ctx.user,
        ctx.project.id,
        "cdc.window.committed",
        window.id,
        {
            "stream_id": stream.id,
            "start_sequence": unit.start_sequence,
            "end_sequence": unit.end_sequence,
            "counts": counts,
        },
    )
    return {"window_id": window.id, "projected_sequence": stream.projected_sequence, **counts}


def read_bounded_events(session, stream):
    events, size = [], 0
    rows = session.scalars(
        select(CdcEvent)
        .where(
            CdcEvent.stream_id == stream.id,
            CdcEvent.tenant_id == stream.tenant_id,
            CdcEvent.sequence > stream.projected_sequence,
        )
        .order_by(CdcEvent.sequence)
        .limit(MAX_EVENTS + 1)
        .execution_options(yield_per=100)
    )
    for event in rows:
        events.append(event)
        size += len(
            json.dumps(event.record, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode(
                "utf-8"
            )
        )
        if size > MAX_BYTES:
            break
    return events


def run_projection(db, settings, run):
    connector = None
    try:
        with db.transaction(run["tenant_id"]) as session:
            _ctx, _job, _stream, _state, binding, source = context_for_run(session, run)
            connector = connector_for(source, settings)
            frozen = {r["resource_id"]: schema_fingerprint(r) for r in binding.resources}
        # No database/project lock while waiting for source I/O. All permissions
        # and the same frozen binding are checked again before any object write.
        discovered = {
            r.resource_id: schema_fingerprint(r.model_dump(mode="json")) for r in connector.discover()
        }
        if any(discovered.get(resource) != schema for resource, schema in frozen.items()):
            raise DomainError(
                409, "source_schema_changed", "来源当前结构与已审核目录不一致，停止投影等待审核"
            )
        result = {}

        def apply(session, job):
            ctx, _, stream, state, binding, _ = context_for_run(session, run)
            events = read_bounded_events(session, stream)
            decision = next_cdc_window(
                [(event.sequence, event.record) for event in events],
                stream.projected_sequence,
                expected_partition=stream.source_partition,
                allow_snapshot=state.baseline_window_id is None,
                expected_snapshot_resources=[r["resource_id"] for r in binding.resources],
                max_events=MAX_EVENTS,
                max_bytes=MAX_BYTES,
            )
            state.observed_sequence = stream.received_sequence
            if decision.status != "ready":
                state.status, state.reason_code = decision.status, decision.code
                state.message = (
                    "等待完整窗口结束证明"
                    if decision.status == "waiting"
                    else "窗口无法安全提交，请审核原因代码；已接收原文保留"
                )
                result.update(
                    status=state.status,
                    reason_code=state.reason_code,
                    projected_sequence=stream.projected_sequence,
                )
                return
            result.update(project_window(session, ctx, stream, state, binding, decision.unit, events))

        finish(db, run, result=result, apply=apply)
    except (CdcValueError, CdcDecodeError) as exc:
        raise DomainError(409, exc.code, "原生值或历史前值不能无损物化，原始事件保留等待审核") from exc
    finally:
        if connector:
            connector.close()


def record_failure(session, job, exc, message):
    state = session.scalar(
        select(CdcProjectionState)
        .where(CdcProjectionState.stream_id == job.payload["stream_id"])
        .with_for_update()
    )
    if state and state.last_job_id == job.id:
        state.status = "blocked" if isinstance(exc, DomainError) else "failed"
        if isinstance(exc, DomainError) and exc.code == "stream_paused":
            state.status = "waiting"
            # Resuming the already-reviewed stream can retry the same window.
            state.observed_sequence = 0
        state.reason_code = exc.code if isinstance(exc, DomainError) else "projection_failed"
        state.message = message
