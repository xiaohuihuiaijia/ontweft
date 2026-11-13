"""Confirmed relation attributes and their existing-source evidence, without arbitration."""

import json
from copy import deepcopy

from sqlalchemy import select

from ontology.core.domain import (
    active_definition,
    coerce_value,
    row_dict,
    validate_properties,
    visible_evidence,
    wire_value,
)
from ontology.core.errors import DomainError
from ontology.core.scalars import bounded_decimal
from ontology.identity.security import allowed_field
from ontology.objects.models import Entity, RelationRevision


def relation_values_equal(field, left, right):
    """Use the published field semantics without rewriting retained values."""
    if left is None or right is None:
        return left is right
    if field.get("data_type") == "number":
        return bounded_decimal(left) == bounded_decimal(right)
    return coerce_value(field, left) == coerce_value(field, right)


def prepare_relation_properties(definition, values, ctx, *, existing=None, remove_properties=()):
    """A partial explicit edit; absence preserves values, removal is separate from NULL."""
    fields = {prop["key"]: prop for prop in definition.get("properties", [])}
    if set(values) & set(remove_properties):
        raise DomainError(422, "invalid_properties", "同一关系属性不能同时修改和移除")
    for key in set(values) | set(remove_properties):
        prop = fields.get(key)
        if prop is None:
            raise DomainError(422, "invalid_properties", "未定义关系属性", {"field": key})
        if (
            prop.get("source_owned")
            or not allowed_field(prop, ctx.role, "write")
            or not allowed_field(prop, ctx.role)
        ):
            raise DomainError(403, "field_forbidden", "包含不可直接编辑的关系属性", {"field": key})
    result = {**(existing or {}), **values}
    for key in remove_properties:
        result.pop(key, None)
    return validate_properties(definition, result)


def record_relation_revision(session, relation, *, actor_id, model_release_id, reason, operation="updated"):
    """Capture the current state once; callers own revision increments and the write lock."""
    if operation != "created":
        existing = session.scalar(
            select(RelationRevision.id).where(
                RelationRevision.relation_id == relation.id,
                RelationRevision.tenant_id == relation.tenant_id,
                RelationRevision.revision == relation.revision,
            )
        )
        if existing:
            return
    source = session.get(Entity, relation.source_id)
    session.add(
        RelationRevision(
            tenant_id=relation.tenant_id,
            project_id=relation.project_id,
            relation_id=relation.id,
            revision=relation.revision,
            type_key=relation.type_key,
            source_type_key=source.type_key,
            source_id=relation.source_id,
            target_id=relation.target_id,
            properties=deepcopy(relation.properties),
            evidence=deepcopy(relation.evidence),
            manual=relation.manual,
            actor_id=actor_id,
            model_release_id=model_release_id,
            reason=reason,
            operation=operation,
        )
    )


def relation_views(session, ctx, rows, *, history=False):
    from ontology.content_access.evidence import filter_evidence
    from ontology.intelligence.proposal_models import DocumentRelationContribution

    definitions = {item["key"]: item for item in active_definition(session, ctx.project)["types"]}
    sources = (
        {}
        if history
        else dict(
            session.execute(
                select(Entity.id, Entity.type_key).where(
                    Entity.tenant_id == ctx.user.tenant_id,
                    Entity.project_id == ctx.project.id,
                    Entity.id.in_({row.source_id for row in rows}),
                )
            ).all()
        )
    )
    contributions = {}
    if not history and rows:
        for relation_id, evidence in session.execute(
            select(
                DocumentRelationContribution.relation_id,
                DocumentRelationContribution.evidence,
            ).where(
                DocumentRelationContribution.tenant_id == ctx.user.tenant_id,
                DocumentRelationContribution.project_id == ctx.project.id,
                DocumentRelationContribution.relation_id.in_([row.id for row in rows]),
            )
        ):
            contributions.setdefault(relation_id, []).extend(evidence)
    prepared = []
    for row in rows:
        source_type = row.source_type_key if history else sources.get(row.source_id)
        relation = next(
            (
                item
                for item in definitions.get(source_type, {}).get("relations", [])
                if item["key"] == row.type_key
            ),
            {},
        )
        visible = {prop["key"] for prop in relation.get("properties", []) if allowed_field(prop, ctx.role)}
        refs = list(row.evidence) + contributions.get(row.id, [])
        # Field-bound references are redacted before source authorization is evaluated.
        refs = visible_evidence(refs, visible)
        refs = list({json.dumps(ref, sort_keys=True, ensure_ascii=False): ref for ref in refs}.values())
        prepared.append((row, relation, visible, refs))
    filter_evidence(session, ctx, [ref for _, _, _, refs in prepared for ref in refs])
    return [
        {
            **row_dict(row),
            "properties": wire_value({k: v for k, v in row.properties.items() if k in visible}),
            "evidence": filter_evidence(session, ctx, refs),
            "label": definition.get("label", row.type_key),
            "model_release_id": row.model_release_id if history else ctx.project.active_release_id,
            "model_version": ctx.project.version,
            "definition_available": bool(definition),
            "definition": {
                **definition,
                "properties": [prop for prop in definition.get("properties", []) if prop["key"] in visible],
            }
            if definition
            else None,
        }
        for row, definition, visible, refs in prepared
    ]
