"""Human decisions over immutable source evidence; never edits source values."""

import hashlib
import json

from sqlalchemy import func, select

from ontology.core.domain import compare_and_swap, save_entity_revision, validate_source_properties
from ontology.core.errors import DomainError, conflict
from ontology.core.models import now
from ontology.objects.contributions.decision_models import ContributionDecision
from ontology.objects.models import Entity
from ontology.sources.models import SourceIdentity


def fingerprint(value):
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    ).hexdigest()


def latest_decisions(session, project, entity_id):
    scope = (
        ContributionDecision.tenant_id == project.tenant_id,
        ContributionDecision.project_id == project.id,
        ContributionDecision.entity_id == entity_id,
    )
    latest = (
        select(ContributionDecision.field_key, func.max(ContributionDecision.revision).label("revision"))
        .where(*scope)
        .group_by(ContributionDecision.field_key)
        .subquery()
    )
    rows = session.scalars(
        select(ContributionDecision)
        .join(
            latest,
            (ContributionDecision.field_key == latest.c.field_key)
            & (ContributionDecision.revision == latest.c.revision),
        )
        .where(*scope)
    ).all()
    return {row.field_key: row for row in rows}


def checked_rows(session, ctx, entity):
    from ontology.objects.contributions.service import _current_rows

    rows = _current_rows(session, ctx, entity)
    if len(rows) > 1000:
        raise DomainError(422, "contribution_budget", "对象来源贡献超出本次裁决预算，未截断候选后继续处理")
    identities = set(
        session.scalars(
            select(SourceIdentity.id).where(
                SourceIdentity.tenant_id == entity.tenant_id,
                SourceIdentity.project_id == entity.project_id,
                SourceIdentity.entity_id == entity.id,
            )
        )
    )
    if identities != {row.identity_id for row in rows}:
        conflict("对象仍有未核实的旧来源贡献，请先完成来源账本迁移")
    return rows


def field_basis(project, entity, prop, rows):
    return {
        "model_release_id": project.active_release_id,
        "type_key": entity.type_key,
        "property": prop,
        "contributions": [
            {
                "contribution_id": row.id,
                "identity_id": row.identity_id,
                "revision": row.revision,
                "record_id": row.record_id,
                "record_codec": row.record_codec,
                "active": row.active,
                "release_id": row.release_id,
                "mapping_key": row.mapping_key,
                "value": row.mapped_values[prop["key"]],
            }
            for row in sorted(rows, key=lambda row: row.id)
            if prop["key"] in row.mapped_values
        ],
    }


def ledger_fingerprint(rows, decisions):
    return fingerprint(
        {
            "contributions": [
                {
                    "id": row.id,
                    "identity_id": row.identity_id,
                    "revision": row.revision,
                    "record_id": row.record_id,
                    "active": row.active,
                    "release_id": row.release_id,
                    "mapping_key": row.mapping_key,
                }
                for row in sorted(rows, key=lambda row: row.id)
            ],
            "decisions": {key: row.id for key, row in sorted(decisions.items())},
        }
    )


def project_contributions(session, ctx, entity, type_def, *, rows=None, decisions=None, managed_before=()):
    """Pure materialization, shared by preview, decisions, snapshots and CDC.

    A stale selection suspends that field until a new explicit decision. Holds
    are deliberately persistent; source changes cannot lift a human pause.
    """
    from ontology.objects.contributions.service import _current_rows, _same

    rows = _current_rows(session, ctx, entity) if rows is None else rows
    decisions = latest_decisions(session, ctx.project, entity.id) if decisions is None else decisions
    props = {prop["key"]: prop for prop in type_def["properties"]}
    managed = {field for row in rows for field in row.mapped_values} | set(managed_before)
    materialized = {field: value for field, value in entity.properties.items() if field not in managed}
    active_rows = [row for row in rows if row.active]
    candidates, checked_values = {}, {}
    for row in active_rows:
        checked_values[row.id] = validate_source_properties(type_def, row.mapped_values)
        for field, value in checked_values[row.id].items():
            candidates.setdefault(field, []).append((row, value))
    conflicts, states, selected = set(), {}, {}
    for field in sorted(managed | set(decisions)):
        prop = props.get(field)
        if not prop or not prop.get("source_owned"):
            continue
        values = candidates.get(field, [])
        decision = decisions.get(field)
        if decision and decision.operation == "hold":
            conflicts.add(field)
            states[field] = "held"
            materialized.pop(field, None)
            continue
        if decision and decision.operation == "select":
            chosen = next(
                (
                    (row, value)
                    for row, value in values
                    if row.id == decision.selected_contribution_id
                    and row.revision == decision.selected_contribution_revision
                ),
                None,
            )
            valid = fingerprint(field_basis(ctx.project, entity, prop, rows)) == decision.basis_fingerprint
            if not chosen or not valid:
                states[field] = "stale"
                conflicts.add(field)
                materialized.pop(field, None)
                continue
            materialized[field] = chosen[1]
            selected[field] = chosen[1]
            states[field] = "selected"
            continue
        if not values:
            states[field] = "withdrawn"
            continue
        first = values[0][1]
        if not all(_same(prop, first, value) for _, value in values[1:]):
            conflicts.add(field)
            states[field] = "conflict"
        else:
            previous = entity.properties.get(field)
            materialized[field] = (
                previous if field in entity.properties and _same(prop, first, previous) else first
            )
            states[field] = "unopposed"
    required_missing = any(
        prop.get("required") and (prop["key"] not in materialized or materialized[prop["key"]] is None)
        for prop in props.values()
    )
    status = (
        "withdrawn"
        if not active_rows
        else "conflict"
        if conflicts
        else "incomplete"
        if required_missing
        else "active"
    )
    materialized = validate_source_properties(type_def, materialized)
    evidence = [
        ref
        for ref in entity.evidence
        if "source_record_id" not in ref and "contribution_decision_id" not in ref
    ]
    for row in active_rows:
        fields = sorted(
            field
            for field, value in checked_values[row.id].items()
            if field not in conflicts
            and (field not in selected or _same(props[field], value, selected[field]))
        )
        if fields:
            evidence.append(
                {
                    "source_record_id": row.record_id,
                    "contribution_id": row.id,
                    "mapping_key": row.mapping_key,
                    "release_id": row.release_id,
                    "fields": fields,
                }
            )
    for field, decision in sorted(decisions.items()):
        if decision.operation != "clear" and field in props:
            evidence.append(
                {
                    "contribution_decision_id": decision.id,
                    "fields": [field],
                    "decision_state": states.get(field, "stale"),
                }
            )
    return {
        "properties": materialized,
        "data_status": status,
        "evidence": evidence,
        "states": states,
        "conflicts": conflicts,
    }


def materialize_contributions(
    session,
    ctx,
    entity,
    type_def,
    *,
    rows=None,
    decisions=None,
    managed_before=(),
    new_entity=False,
    force=False,
    reason="来源贡献物化",
):
    projected = project_contributions(
        session, ctx, entity, type_def, rows=rows, decisions=decisions, managed_before=managed_before
    )
    changed = (
        force
        or new_entity
        or entity.properties != projected["properties"]
        or entity.data_status != projected["data_status"]
        or entity.release_id != ctx.project.active_release_id
    )
    if changed:
        values = {key: projected[key] for key in ("properties", "data_status", "evidence")}
        values.update(release_id=ctx.project.active_release_id, updated_at=now())
        if new_entity:
            for key, value in values.items():
                setattr(entity, key, value)
        else:
            compare_and_swap(session, entity, entity.revision, values)
        save_entity_revision(session, entity, ctx.user, reason)
    session.flush()
    return changed


def recheck_decisions_after_publish(session, ctx):
    """Publishing cannot leave a previous model's human selection active."""
    from ontology.core.domain import type_definition

    entity_ids = session.scalars(
        select(ContributionDecision.entity_id)
        .where(
            ContributionDecision.tenant_id == ctx.project.tenant_id,
            ContributionDecision.project_id == ctx.project.id,
        )
        .distinct()
    ).all()
    for entity_id in entity_ids:
        entity = session.get(Entity, entity_id)
        if not entity or entity.deleted:
            continue
        decisions = latest_decisions(session, ctx.project, entity.id)
        if not any(row.operation != "clear" for row in decisions.values()):
            continue
        rows = checked_rows(session, ctx, entity)
        materialize_contributions(
            session,
            ctx,
            entity,
            type_definition(session, ctx.project, entity.type_key),
            rows=rows,
            decisions=decisions,
            reason="模型发布后重新评估来源裁决",
        )
