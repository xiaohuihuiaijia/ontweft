"""Authorized graph slices: the database owns scale; the canvas stays bounded."""

from fastapi import APIRouter, Depends, Query, Request
from pydantic import Field
from sqlalchemy import or_, select

from ontology.core.domain import active_definition, entity_view, scoped_record
from ontology.core.errors import conflict, missing
from ontology.core.schemas import StrictModel
from ontology.identity.security import access, authenticated
from ontology.objects.models import Entity, Relation

router = APIRouter()


def visible_entities(ctx):
    return (
        Entity.tenant_id == ctx.user.tenant_id,
        Entity.project_id == ctx.project.id,
        Entity.deleted.is_(False),
        Entity.data_status == "active",
    )


def graph_slice(session, ctx, rows, rels, *, truncated=False):
    definitions = {t["key"]: t for t in active_definition(session, ctx.project)["types"]}
    nodes, object_types = [], {obj.id: obj.type_key for obj in rows}
    for obj in rows:
        definition = definitions.get(obj.type_key)
        if not definition:
            continue
        props = entity_view(session, obj, ctx, definition=definition)["properties"]
        nodes.append(
            {
                "id": obj.id,
                "label": str(props.get("name", props.get("title", obj.type_key))),
                "type_key": obj.type_key,
                "type_label": definition["label"],
                "kind": "object",
                "properties": props,
                "revision": obj.revision,
            }
        )
    ids = {n["id"] for n in nodes}
    edges = []
    for rel in rels[:1000]:
        if rel.source_id not in ids or rel.target_id not in ids:
            continue
        definition = next(
            (
                r
                for r in definitions[object_types[rel.source_id]].get("relations", [])
                if r["key"] == rel.type_key
            ),
            {},
        )
        edges.append(
            {
                "id": rel.id,
                "source": rel.source_id,
                "target": rel.target_id,
                "label": definition.get("label", rel.type_key),
                "type_key": rel.type_key,
                "kind": "relation",
                "cardinality": definition.get("cardinality"),
            }
        )
    return {
        "release_id": ctx.project.active_release_id,
        "version": ctx.project.version,
        "nodes": nodes,
        "edges": edges,
        "truncated": truncated or len(rels) > 1000,
    }


@router.get("/graph/neighbors")
def neighbors(
    request: Request,
    project_id: str,
    object_id: str,
    cursor: str = Query("", max_length=36),
    expected_model_release_id: str | None = None,
    limit: int = Query(50, ge=1, le=100),
    user=Depends(authenticated),
):
    with request.app.state.db.transaction(user.tenant_id) as session:
        ctx = access(session, user, project_id)
        if expected_model_release_id and expected_model_release_id != ctx.project.active_release_id:
            conflict("模型版本已改变，请刷新图谱后重新展开")
        root = scoped_record(session, Entity, object_id, user)
        if root.project_id != project_id or root.deleted or root.data_status != "active":
            missing()
        active = select(Entity.id).where(*visible_entities(ctx))
        rels = session.scalars(
            select(Relation)
            .where(
                Relation.tenant_id == user.tenant_id,
                Relation.project_id == project_id,
                or_(Relation.source_id == object_id, Relation.target_id == object_id),
                Relation.source_id.in_(active),
                Relation.target_id.in_(active),
                Relation.id > cursor,
            )
            .order_by(Relation.id)
            .limit(limit + 1)
        ).all()
        page = rels[:limit]
        ids = {object_id} | {r.source_id for r in page} | {r.target_id for r in page}
        rows = session.scalars(select(Entity).where(*visible_entities(ctx), Entity.id.in_(ids))).all()
        return {
            **graph_slice(session, ctx, rows, page),
            "next_cursor": page[-1].id if len(rels) > limit else None,
        }


class GraphNodes(StrictModel):
    project_id: str
    ids: list[str] = Field(max_length=500)


@router.post("/graph/nodes")
def selected_nodes(data: GraphNodes, request: Request, user=Depends(authenticated)):
    with request.app.state.db.transaction(user.tenant_id) as session:
        ctx = access(session, user, data.project_id)
        rows = session.scalars(select(Entity).where(*visible_entities(ctx), Entity.id.in_(data.ids))).all()
        ids = [obj.id for obj in rows]
        rels = session.scalars(
            select(Relation)
            .where(
                Relation.tenant_id == user.tenant_id,
                Relation.project_id == data.project_id,
                Relation.source_id.in_(ids),
                Relation.target_id.in_(ids),
            )
            .order_by(Relation.id)
            .limit(1001)
        ).all()
        return graph_slice(session, ctx, rows, rels)
