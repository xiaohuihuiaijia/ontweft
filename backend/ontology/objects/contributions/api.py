"""Field-authorized source provenance, without cached native row contents."""

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy import select

from ontology.core.domain import scoped_record, type_definition, wire_value
from ontology.core.errors import missing
from ontology.identity.security import access, allowed_field, authenticated
from ontology.objects.contributions.models import ContributionRevision, SourceContribution
from ontology.objects.models import Entity
from ontology.sources.models import SourceIdentity

router = APIRouter()


def contribution_view(row, visible):
    return {
        "id": row.id,
        "identity_id": row.identity_id,
        "revision": row.revision,
        "record_id": row.record_id,
        "record_codec": row.record_codec,
        "release_id": row.release_id,
        "mapping_key": row.mapping_key,
        "active": row.active,
        "withdraw_reason": row.withdraw_reason,
        "stream_id": row.stream_id,
        "sequence": row.sequence,
        "created_at": row.created_at,
        "mapped_values": wire_value(
            {key: value for key, value in row.mapped_values.items() if key in visible}
        ),
    }


@router.get("/objects/{entity_id}/contributions")
def contributions(
    entity_id: str,
    request: Request,
    after: str | None = None,
    limit: int = Query(50, ge=1, le=100),
    user=Depends(authenticated),
):
    with request.app.state.db.transaction(user.tenant_id) as session:
        entity = scoped_record(session, Entity, entity_id, user)
        ctx = access(session, user, entity.project_id, "sources:write")
        access(session, user, entity.project_id, "objects:read")
        session.refresh(entity)
        if entity.deleted:
            missing()
        visible = {
            p["key"]
            for p in type_definition(session, ctx.project, entity.type_key)["properties"]
            if allowed_field(p, ctx.role)
        }
        query = (
            select(SourceContribution, SourceIdentity)
            .join(SourceIdentity, SourceIdentity.id == SourceContribution.identity_id)
            .where(
                SourceIdentity.entity_id == entity.id,
                SourceIdentity.tenant_id == user.tenant_id,
                SourceIdentity.project_id == entity.project_id,
                SourceContribution.tenant_id == user.tenant_id,
                SourceContribution.project_id == entity.project_id,
            )
        )
        if after:
            query = query.where(SourceContribution.id > after)
        rows = session.execute(query.order_by(SourceContribution.id).limit(limit + 1)).all()
        return {
            "data_status": entity.data_status,
            "items": [
                {
                    **contribution_view(row, visible),
                    "source_id": identity.source_id,
                    "resource_id": identity.resource_id,
                }
                for row, identity in rows[:limit]
            ],
            "next_cursor": rows[limit - 1][0].id if len(rows) > limit else None,
        }


@router.get("/objects/{entity_id}/contributions/{contribution_id}/history")
def history(
    entity_id: str,
    contribution_id: str,
    request: Request,
    before: int | None = Query(None, ge=1),
    limit: int = Query(50, ge=1, le=100),
    user=Depends(authenticated),
):
    with request.app.state.db.transaction(user.tenant_id) as session:
        entity = scoped_record(session, Entity, entity_id, user)
        ctx = access(session, user, entity.project_id, "sources:write")
        access(session, user, entity.project_id, "objects:read")
        session.refresh(entity)
        if entity.deleted:
            missing()
        contribution = scoped_record(session, SourceContribution, contribution_id, user)
        identity = scoped_record(session, SourceIdentity, contribution.identity_id, user)
        if identity.entity_id != entity.id or contribution.project_id != entity.project_id:
            missing()
        visible = {
            p["key"]
            for p in type_definition(session, ctx.project, entity.type_key)["properties"]
            if allowed_field(p, ctx.role)
        }
        query = select(ContributionRevision).where(
            ContributionRevision.tenant_id == user.tenant_id,
            ContributionRevision.project_id == entity.project_id,
            ContributionRevision.contribution_id == contribution.id,
        )
        if before is not None:
            query = query.where(ContributionRevision.revision < before)
        rows = session.scalars(query.order_by(ContributionRevision.revision.desc()).limit(limit + 1)).all()
        return {
            "items": [contribution_view(row, visible) for row in rows[:limit]],
            "next_cursor": rows[limit - 1].revision if len(rows) > limit else None,
        }
