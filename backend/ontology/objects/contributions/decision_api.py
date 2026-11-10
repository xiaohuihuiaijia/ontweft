"""Preview and commit source decisions using current permissions and full baselines."""

from types import SimpleNamespace
from typing import Literal

from fastapi import APIRouter, Depends, Query, Request
from pydantic import Field, model_validator
from sqlalchemy import select

from ontology.core.domain import entity_view, scoped_record, type_definition, wire_value
from ontology.core.errors import DomainError, conflict, forbidden, missing
from ontology.core.schemas import StrictModel
from ontology.identity.security import access, allowed_field, audit, authenticated
from ontology.objects.contributions.decision_models import ContributionDecision
from ontology.objects.contributions.decisions import (
    checked_rows,
    field_basis,
    fingerprint,
    latest_decisions,
    ledger_fingerprint,
    materialize_contributions,
    project_contributions,
)
from ontology.objects.models import Entity
from ontology.sources.models import Source, SourceIdentity

router = APIRouter()


class DecisionInput(StrictModel):
    expected_revision: int = Field(ge=1, strict=True)
    expected_model_release_id: str = Field(min_length=1, max_length=36)
    expected_contribution_fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")
    field_key: str = Field(pattern=r"^[a-zA-Z][a-zA-Z0-9_]{0,79}$")
    operation: Literal["select", "hold", "clear"]
    selected_contribution_id: str | None = Field(default=None, min_length=1, max_length=36)
    reason: str = Field(min_length=1, max_length=2000)

    @model_validator(mode="after")
    def selection(self):
        if (self.operation == "select") != (self.selected_contribution_id is not None):
            raise ValueError("选择来源需要贡献标识，其余操作不得指定贡献")
        return self


class DecisionCommit(DecisionInput):
    expected_preview_fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")


def context(session, user, entity_id, *, write=False):
    entity = scoped_record(session, Entity, entity_id, user)
    ctx = access(session, user, entity.project_id, "sources:write")
    access(session, user, entity.project_id, "objects:write" if write else "objects:read")
    session.refresh(entity)
    if entity.deleted:
        missing()
    type_def = type_definition(session, ctx.project, entity.type_key)
    rows = checked_rows(session, ctx, entity)
    decisions = latest_decisions(session, ctx.project, entity.id)
    return ctx, entity, type_def, rows, decisions


def property_for(type_def, field_key, ctx, *, write=False):
    prop = next((p for p in type_def["properties"] if p["key"] == field_key), None)
    if not prop or not prop.get("source_owned") or not allowed_field(prop, ctx.role):
        missing()
    if write and not allowed_field(prop, ctx.role, "write"):
        forbidden()
    return prop


def decision_view(row):
    return {
        key: getattr(row, key)
        for key in (
            "id",
            "field_key",
            "revision",
            "operation",
            "reason",
            "actor_id",
            "created_at",
            "selected_contribution_id",
            "selected_contribution_revision",
            "model_release_id",
            "entity_revision",
        )
    }


def value_view(values, key):
    return {"present": key in values, **({"value": wire_value(values[key])} if key in values else {})}


def prepare(session, ctx, entity, type_def, rows, decisions, data):
    prop = property_for(type_def, data.field_key, ctx, write=True)
    if (entity.revision, ctx.project.active_release_id, ledger_fingerprint(rows, decisions)) != (
        data.expected_revision,
        data.expected_model_release_id,
        data.expected_contribution_fingerprint,
    ):
        conflict("对象、模型或来源贡献已改变，请保留选择与理由，刷新核对后重新预览")
    basis = field_basis(ctx.project, entity, prop, rows)
    if not basis["contributions"]:
        raise DomainError(422, "no_contribution", "该字段没有可核对的来源贡献")
    previous = decisions.get(data.field_key)
    if data.operation == "clear" and (not previous or previous.operation == "clear"):
        conflict("该字段没有需要撤销的人为决定")
    chosen = next((row for row in rows if row.id == data.selected_contribution_id), None)
    if data.operation == "select" and (
        not chosen or not chosen.active or data.field_key not in chosen.mapped_values
    ):
        raise DomainError(422, "invalid_selection", "只能选择当前对象该字段的活动来源贡献")
    proposed = SimpleNamespace(
        id="preview",
        field_key=data.field_key,
        operation=data.operation,
        selected_contribution_id=data.selected_contribution_id,
        selected_contribution_revision=chosen.revision if chosen else None,
        basis_fingerprint=fingerprint(basis),
    )
    projected = project_contributions(
        session, ctx, entity, type_def, rows=rows, decisions={**decisions, data.field_key: proposed}
    )
    visible = {p["key"] for p in type_def["properties"] if allowed_field(p, ctx.role)}
    preview = {
        "field_key": data.field_key,
        "operation": data.operation,
        "before": value_view(entity.properties, data.field_key),
        "after": value_view(projected["properties"], data.field_key),
        "data_status_before": entity.data_status,
        "data_status_after": projected["data_status"],
        "remaining_visible_conflicts": sorted(projected["conflicts"] & visible),
        "scope": "platform_only",
    }
    # Never expose a plain hash of hidden property values. Full current ledger
    # membership/revisions are bound separately; visible results bind this preview.
    request_data = data.model_dump(exclude={"expected_preview_fingerprint"})
    preview["preview_fingerprint"] = fingerprint(
        {"request": request_data, "preview": preview, "user_id": ctx.user.id, "role": ctx.role}
    )
    return basis, proposed, preview


@router.get("/objects/{entity_id}/contribution-decisions")
def decision_context(entity_id: str, request: Request, user=Depends(authenticated)):
    with request.app.state.db.transaction(user.tenant_id) as session:
        ctx, entity, type_def, rows, decisions = context(session, user, entity_id)
        projected = project_contributions(session, ctx, entity, type_def, rows=rows, decisions=decisions)
        identities = {
            row.id: row
            for row in session.scalars(
                select(SourceIdentity).where(
                    SourceIdentity.tenant_id == user.tenant_id,
                    SourceIdentity.project_id == entity.project_id,
                    SourceIdentity.entity_id == entity.id,
                )
            )
        }
        source_names = dict(
            session.execute(
                select(Source.id, Source.name).where(
                    Source.tenant_id == user.tenant_id,
                    Source.project_id == entity.project_id,
                    Source.id.in_({identity.source_id for identity in identities.values()}),
                )
            ).all()
        )
        fields = []
        for prop in type_def["properties"]:
            key = prop["key"]
            if not prop.get("source_owned") or not allowed_field(prop, ctx.role):
                continue
            candidates = [
                {
                    "contribution_id": row.id,
                    "revision": row.revision,
                    "identity_id": row.identity_id,
                    "source_id": identities[row.identity_id].source_id,
                    "source_name": source_names.get(identities[row.identity_id].source_id),
                    "resource_id": identities[row.identity_id].resource_id,
                    "record_id": row.record_id,
                    "release_id": row.release_id,
                    "mapping_key": row.mapping_key,
                    "active": row.active,
                    "value": wire_value(row.mapped_values[key]),
                }
                for row in rows
                if key in row.mapped_values
            ]
            if not candidates and key not in decisions:
                continue
            fields.append(
                {
                    "field_key": key,
                    "label": prop["label"],
                    "data_type": prop["data_type"],
                    "unit": prop.get("unit"),
                    "state": projected["states"].get(key, "withdrawn"),
                    "can_decide": "objects:write" in ctx.permissions
                    and allowed_field(prop, ctx.role, "write"),
                    "current_present": key in entity.properties,
                    **(
                        {"current_value": wire_value(entity.properties[key])}
                        if key in entity.properties
                        else {}
                    ),
                    "decision": decision_view(decisions[key]) if key in decisions else None,
                    "candidates": candidates,
                }
            )
        return {
            "object_id": entity.id,
            "expected_revision": entity.revision,
            "expected_model_release_id": ctx.project.active_release_id,
            "expected_contribution_fingerprint": ledger_fingerprint(rows, decisions),
            "data_status": entity.data_status,
            "fields": fields,
        }


@router.post("/objects/{entity_id}/contribution-decisions/preview")
def preview_decision(entity_id: str, data: DecisionInput, request: Request, user=Depends(authenticated)):
    with request.app.state.db.transaction(user.tenant_id) as session:
        ctx, entity, type_def, rows, decisions = context(session, user, entity_id, write=True)
        return prepare(session, ctx, entity, type_def, rows, decisions, data)[2]


@router.post("/objects/{entity_id}/contribution-decisions", status_code=201)
def commit_decision(entity_id: str, data: DecisionCommit, request: Request, user=Depends(authenticated)):
    with request.app.state.db.transaction(user.tenant_id) as session:
        ctx, entity, type_def, rows, decisions = context(session, user, entity_id, write=True)
        basis, proposed, preview = prepare(session, ctx, entity, type_def, rows, decisions, data)
        if data.expected_preview_fingerprint != preview["preview_fingerprint"]:
            conflict("裁决内容或可见范围与预览不一致，请重新预览")
        previous = decisions.get(data.field_key)
        decision = ContributionDecision(
            tenant_id=user.tenant_id,
            project_id=entity.project_id,
            entity_id=entity.id,
            field_key=data.field_key,
            revision=previous.revision + 1 if previous else 1,
            operation=data.operation,
            selected_contribution_id=proposed.selected_contribution_id,
            selected_contribution_revision=proposed.selected_contribution_revision,
            model_release_id=ctx.project.active_release_id,
            entity_revision=entity.revision,
            basis=basis,
            basis_fingerprint=proposed.basis_fingerprint,
            preview_fingerprint=preview["preview_fingerprint"],
            reason=data.reason,
            actor_id=user.id,
        )
        session.add(decision)
        session.flush()
        materialize_contributions(
            session,
            ctx,
            entity,
            type_def,
            rows=rows,
            decisions={**decisions, data.field_key: decision},
            force=True,
            reason=f"来源裁决 {decision.id}",
        )
        audit(
            session,
            user,
            entity.project_id,
            "contribution.decision",
            entity.id,
            {"decision_id": decision.id, "revision": entity.revision, "operation": decision.operation},
        )
        return {"decision": decision_view(decision), "object": entity_view(session, entity, ctx)}


@router.get("/objects/{entity_id}/contribution-decisions/{field_key}/history")
def decision_history(
    entity_id: str,
    field_key: str,
    request: Request,
    before: int | None = Query(None, ge=1),
    limit: int = Query(25, ge=1, le=100),
    user=Depends(authenticated),
):
    with request.app.state.db.transaction(user.tenant_id) as session:
        ctx, entity, type_def, _, _ = context(session, user, entity_id)
        property_for(type_def, field_key, ctx)
        query = select(ContributionDecision).where(
            ContributionDecision.tenant_id == user.tenant_id,
            ContributionDecision.project_id == entity.project_id,
            ContributionDecision.entity_id == entity.id,
            ContributionDecision.field_key == field_key,
        )
        if before is not None:
            query = query.where(ContributionDecision.revision < before)
        rows = session.scalars(query.order_by(ContributionDecision.revision.desc()).limit(limit + 1)).all()
        return {
            "items": [decision_view(row) for row in rows[:limit]],
            "next_cursor": rows[limit - 1].revision if len(rows) > limit else None,
        }
