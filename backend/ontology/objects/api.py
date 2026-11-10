import operator
from decimal import Decimal
from typing import Literal

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy import Numeric, cast, func, or_, select

from ontology.audit.models import Audit
from ontology.core.domain import (
    active_definition,
    coerce_value,
    compare_and_swap,
    entity_view,
    row_dict,
    save_entity_revision,
    scoped_record,
    type_definition,
    validate_properties,
    visible_evidence,
    wire_value,
)
from ontology.core.errors import DomainError, conflict, forbidden, missing
from ontology.core.models import now
from ontology.identity.security import access, allowed_field, audit, authenticated
from ontology.objects.models import Entity, EntityRevision, Relation
from ontology.objects.schemas import ModelRevisionInput, ObjectInput, ObjectQuery, ObjectUpdate, RelationInput
from ontology.objects.status import require_active_entity

router = APIRouter()


def ensure_model_baseline(session, ctx, data):
    # Each caller has acquired access(..., 'objects:write'), hence the project
    # lock shared with publication. Refresh even if the identity map had loaded
    # the project before waiting for that lock; the lock lasts until commit.
    session.refresh(ctx.project, attribute_names=["active_release_id", "version"])
    if (
        "expected_model_release_id" in data.model_fields_set
        and data.expected_model_release_id != ctx.project.active_release_id
    ):
        conflict("对象编辑所依据的模型已改变，请保留草稿并重新读取当前模型后核对")


def field_expression(prop):
    value = Entity.properties[prop["key"]]
    if prop["data_type"] in {"integer", "number"}:
        return cast(value.as_string(), Numeric())
    if prop["data_type"] == "boolean":
        return value.as_boolean()
    return value.as_string()


def ensure_writable(type_def, properties, role):
    for prop in type_def.get("properties", []):
        if prop["key"] in properties and (prop.get("source_owned") or not allowed_field(prop, role, "write")):
            raise DomainError(403, "field_forbidden", "包含不可直接编辑的属性", {"field": prop["key"]})


def query_objects(session, ctx, query: ObjectQuery):
    if query.release_id and query.release_id != ctx.project.active_release_id:
        conflict("查询本体版本已过期，请刷新")
    type_def = type_definition(session, ctx.project, query.type_key)
    props = {p["key"]: p for p in type_def["properties"]}
    conditions = [
        Entity.tenant_id == ctx.user.tenant_id,
        Entity.project_id == ctx.project.id,
        Entity.type_key == query.type_key,
        Entity.deleted.is_(False),
        Entity.data_status == "active",
    ]
    for item in query.filters:
        prop = props.get(item.field)
        if not prop or not allowed_field(prop, ctx.role):
            forbidden()
        if prop["data_type"] == "json":
            raise DomainError(422, "unsupported_filter", "JSON 属性需要先定义可查询的类型化字段")
        expression = field_expression(prop)
        if item.op == "is_null":
            conditions.append(expression.is_(None))
            continue
        if item.op == "in":
            if not isinstance(item.value, list) or len(item.value) > 100:
                raise DomainError(422, "invalid_filter", "in 筛选需要最多 100 个值")
            try:
                values = [coerce_value({**prop, "required": False}, x) for x in item.value]
            except (ValueError, TypeError):
                raise DomainError(422, "invalid_filter", "筛选值类型不匹配")
            if prop["data_type"] in {"integer", "number"}:
                values = [Decimal(value) if value is not None else None for value in values]
            conditions.append(expression.in_(values))
            continue
        try:
            value = coerce_value({**prop, "required": False}, item.value)
        except (ValueError, TypeError):
            raise DomainError(422, "invalid_filter", "筛选值类型不匹配")
        if prop["data_type"] in {"integer", "number"} and value is not None:
            value = Decimal(value)
        if item.op == "contains":
            if prop["data_type"] not in {"string", "enum"} or not isinstance(value, str):
                raise DomainError(422, "invalid_filter", "包含筛选仅适用于文本")
            conditions.append(expression.contains(value, autoescape=True))
        elif value is None:
            if item.op not in {"eq", "ne"}:
                raise DomainError(422, "invalid_filter", "null 不可用于大小比较")
            conditions.append(expression.is_(None) if item.op == "eq" else expression.is_not(None))
        else:
            operators = {
                "eq": operator.eq,
                "ne": operator.ne,
                "gt": operator.gt,
                "gte": operator.ge,
                "lt": operator.lt,
                "lte": operator.le,
            }
            conditions.append(operators[item.op](expression, value))
    order = Entity.id
    if query.sort_field:
        prop = props.get(query.sort_field)
        if not prop or not allowed_field(prop, ctx.role):
            forbidden()
        if prop["data_type"] == "json":
            raise DomainError(422, "unsupported_sort", "JSON 属性不可直接排序")
        order = field_expression(prop)
    total = session.scalar(select(func.count()).select_from(Entity).where(*conditions))
    objects = session.scalars(
        select(Entity)
        .where(*conditions)
        .order_by(order.desc() if query.sort_desc else order, Entity.id)
        .offset(query.offset)
        .limit(query.limit)
    ).all()
    return {
        "items": [entity_view(session, obj, ctx) for obj in objects],
        "total": total,
        "release_id": ctx.project.active_release_id,
        "version": ctx.project.version,
    }


@router.post("/query")
def query(data: ObjectQuery, request: Request, user=Depends(authenticated)):
    with request.app.state.db.transaction(user.tenant_id) as session:
        ctx = access(session, user, data.project_id)
        return query_objects(session, ctx, data)


@router.get("/objects")
def objects(
    request: Request,
    project_id: str,
    type_key: str | None = None,
    q: str = "",
    data_status: Literal["active", "conflict", "incomplete", "withdrawn", "attention"] = "active",
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0, le=1000000),
    user=Depends(authenticated),
):
    with request.app.state.db.transaction(user.tenant_id) as session:
        # Diagnostic reads acquire the shared mutation scope before reading the
        # membership, so a revocation while waiting cannot reuse a cached role.
        ctx = access(
            session, user, project_id, "sources:write" if data_status != "active" else "objects:read"
        )
        if data_status != "active":
            access(session, user, project_id, "objects:read")
        if type_key and not q and data_status == "active":
            return query_objects(
                session,
                ctx,
                ObjectQuery(project_id=project_id, type_key=type_key, limit=limit, offset=offset),
            )
        conditions = [
            Entity.tenant_id == user.tenant_id,
            Entity.project_id == project_id,
            Entity.deleted.is_(False),
            Entity.data_status.in_(["conflict", "incomplete", "withdrawn"])
            if data_status == "attention"
            else Entity.data_status == data_status,
        ]
        definitions = active_definition(session, ctx.project)["types"]
        if type_key:
            definitions = [type_definition(session, ctx.project, type_key)]
            conditions.append(Entity.type_key == type_key)
        if q:
            if len(q) > 200:
                raise DomainError(422, "invalid_search", "搜索词过长")
            terms = []
            for definition in definitions:
                for prop in definition["properties"]:
                    if prop["data_type"] in {"string", "enum"} and allowed_field(prop, ctx.role):
                        terms.append(
                            (Entity.type_key == definition["key"])
                            & Entity.properties[prop["key"]].as_string().contains(q, autoescape=True)
                        )
            if not terms:
                return {"items": [], "total": 0}
            conditions.append(or_(*terms))
        total = session.scalar(select(func.count()).select_from(Entity).where(*conditions))
        rows = session.scalars(
            select(Entity)
            .where(*conditions)
            .order_by(Entity.created_at.desc(), Entity.id)
            .offset(offset)
            .limit(limit)
        ).all()
        return {
            "items": [entity_view(session, x, ctx) for x in rows],
            "total": total,
            "release_id": ctx.project.active_release_id,
        }


@router.post("/objects", status_code=201)
def create_object(data: ObjectInput, request: Request, user=Depends(authenticated)):
    with request.app.state.db.transaction(user.tenant_id) as session:
        ctx = access(session, user, data.project_id, "objects:write")
        ensure_model_baseline(session, ctx, data)
        definition = type_definition(session, ctx.project, data.type_key)
        ensure_writable(definition, data.properties, ctx.role)
        values = validate_properties(definition, data.properties)
        if data.evidence:
            from ontology.knowledge.references import validate_references

            validate_references(session, ctx, data.evidence)
        obj = Entity(
            tenant_id=user.tenant_id,
            project_id=data.project_id,
            type_key=data.type_key,
            properties=values,
            evidence=data.evidence,
            release_id=ctx.project.active_release_id,
        )
        session.add(obj)
        session.flush()
        save_entity_revision(session, obj, user)
        audit(session, user, data.project_id, "object.created", obj.id)
        return entity_view(session, obj, ctx)


@router.get("/objects/{object_id}")
def get_object(object_id: str, request: Request, user=Depends(authenticated)):
    with request.app.state.db.transaction(user.tenant_id) as session:
        obj = scoped_record(session, Entity, object_id, user)
        ctx = access(session, user, obj.project_id)
        if obj.deleted:
            missing()
        return entity_view(session, obj, ctx)


@router.patch("/objects/{object_id}")
def update_object(object_id: str, data: ObjectUpdate, request: Request, user=Depends(authenticated)):
    with request.app.state.db.transaction(user.tenant_id) as session:
        obj = scoped_record(session, Entity, object_id, user)
        ctx = access(session, user, obj.project_id, "objects:write")
        ensure_model_baseline(session, ctx, data)
        session.refresh(obj)
        if obj.deleted:
            missing()
        require_active_entity(obj)
        definition = type_definition(session, ctx.project, obj.type_key)
        ensure_writable(definition, data.properties, ctx.role)
        values = validate_properties(definition, {**obj.properties, **data.properties})
        compare_and_swap(
            session,
            obj,
            data.expected_revision,
            {"properties": values, "updated_at": now(), "release_id": ctx.project.active_release_id},
        )
        save_entity_revision(session, obj, user, data.reason)
        audit(session, user, obj.project_id, "object.edited", obj.id, {"revision": obj.revision})
        return entity_view(session, obj, ctx)


@router.get("/objects/{object_id}/history")
def object_history(object_id: str, request: Request, user=Depends(authenticated)):
    with request.app.state.db.transaction(user.tenant_id) as session:
        obj = scoped_record(session, Entity, object_id, user)
        ctx = access(session, user, obj.project_id)
        if obj.deleted:
            missing()
        visible = {
            p["key"]
            for p in type_definition(session, ctx.project, obj.type_key)["properties"]
            if allowed_field(p, ctx.role)
        }
        rows = session.scalars(
            select(EntityRevision)
            .where(EntityRevision.entity_id == object_id, EntityRevision.tenant_id == user.tenant_id)
            .order_by(EntityRevision.revision.desc())
        ).all()
        return {
            "items": [
                {
                    **row_dict(x),
                    "properties": wire_value({k: v for k, v in x.properties.items() if k in visible}),
                    "evidence": visible_evidence(x.evidence, visible),
                }
                for x in rows
            ],
            "total": len(rows),
        }


@router.get("/relations")
def relations(
    request: Request,
    project_id: str,
    object_id: str | None = None,
    limit: int = Query(200, ge=1, le=1000),
    user=Depends(authenticated),
):
    with request.app.state.db.transaction(user.tenant_id) as session:
        access(session, user, project_id)
        active_ids = select(Entity.id).where(
            Entity.tenant_id == user.tenant_id,
            Entity.project_id == project_id,
            Entity.deleted.is_(False),
            Entity.data_status == "active",
        )
        conditions = [
            Relation.tenant_id == user.tenant_id,
            Relation.project_id == project_id,
            Relation.source_id.in_(active_ids),
            Relation.target_id.in_(active_ids),
        ]
        if object_id:
            conditions.append(or_(Relation.source_id == object_id, Relation.target_id == object_id))
        rows = session.scalars(select(Relation).where(*conditions).order_by(Relation.id).limit(limit)).all()
        return {
            "items": [row_dict(x) for x in rows],
            "total": session.scalar(select(func.count()).select_from(Relation).where(*conditions)),
        }


@router.post("/relations", status_code=201)
def create_relation(data: RelationInput, request: Request, user=Depends(authenticated)):
    with request.app.state.db.transaction(user.tenant_id, write=True) as session:
        ctx = access(session, user, data.project_id, "objects:write")
        ensure_model_baseline(session, ctx, data)
        source = scoped_record(session, Entity, data.source_id, user)
        target = scoped_record(session, Entity, data.target_id, user)
        if (
            source.project_id != data.project_id
            or target.project_id != data.project_id
            or source.deleted
            or target.deleted
        ):
            missing()
        require_active_entity(source)
        require_active_entity(target)
        definition = type_definition(session, ctx.project, source.type_key)
        relation = next((r for r in definition.get("relations", []) if r["key"] == data.type_key), None)
        if not relation or relation["target_type"] != target.type_key:
            raise DomainError(422, "invalid_relation", "关系谓词或目标类型不匹配")
        existing = session.scalar(
            select(Relation).where(
                Relation.tenant_id == user.tenant_id,
                Relation.project_id == data.project_id,
                Relation.source_id == source.id,
                Relation.target_id == target.id,
                Relation.type_key == data.type_key,
            )
        )
        if existing and not existing.manual:
            existing.manual = True
            existing.revision += 1
            audit(session, user, data.project_id, "relation.manual.retained", existing.id)
            return row_dict(existing)
        if relation["cardinality"] == "one" and session.scalar(
            select(Relation.id).where(
                Relation.project_id == data.project_id,
                Relation.source_id == source.id,
                Relation.type_key == data.type_key,
            )
        ):
            conflict("该关系只允许一个目标")
        if data.properties:
            raise DomainError(422, "undefined_relation_properties", "请先定义关系属性；当前关系没有可写属性")
        if data.evidence:
            from ontology.knowledge.references import validate_references

            validate_references(session, ctx, data.evidence)
        row = Relation(
            tenant_id=user.tenant_id,
            project_id=data.project_id,
            type_key=data.type_key,
            source_id=source.id,
            target_id=target.id,
            properties={},
            evidence=data.evidence,
        )
        session.add(row)
        from ontology.objects.constraints import touch_entity

        touch_entity(session, source)
        session.flush()
        audit(session, user, data.project_id, "relation.created", row.id)
        return row_dict(row)


@router.delete("/relations/{relation_id}")
def delete_relation(
    relation_id: str, data: ModelRevisionInput, request: Request, user=Depends(authenticated)
):
    with request.app.state.db.transaction(user.tenant_id, write=True) as session:
        row = scoped_record(session, Relation, relation_id, user)
        ctx = access(session, user, row.project_id, "objects:write")
        ensure_model_baseline(session, ctx, data)
        if row.revision != data.expected_revision:
            conflict()
        from ontology.objects.rule_models import RelationContribution

        supported = session.scalar(
            select(RelationContribution.id).where(RelationContribution.relation_id == row.id).limit(1)
        )
        if supported:
            if not row.manual:
                conflict("关系由持续规则维护，请调整规则或匹配字段")
            row.manual = False
            row.revision += 1
            audit(session, user, row.project_id, "relation.manual.removed", row.id)
            return {"ok": True, "retained_by_rules": True}
        from ontology.objects.constraints import touch_entity

        touch_entity(session, session.get(Entity, row.source_id))
        source = session.get(Entity, row.source_id)
        session.delete(row)
        session.flush()
        from ontology.objects.relation_rules import changed_entity

        changed_entity(session, source)
        audit(session, user, row.project_id, "relation.deleted", row.id)
        return {"ok": True}


@router.get("/graph")
async def graph(
    request: Request,
    project_id: str,
    kind: str = "instances",
    limit: int = Query(200, ge=1, le=500),
    user=Depends(authenticated),
):
    if kind == "knowledge":
        if "knowledge" not in request.app.state.settings.extensions:
            raise DomainError(409, "extension_disabled", "当前部署未启用知识资料扩展")
        from ontology.content_access.service import refresh_project

        await refresh_project(request, user, project_id)
    with request.app.state.db.transaction(user.tenant_id) as session:
        ctx = access(session, user, project_id)
        graph_baseline = {"release_id": ctx.project.active_release_id, "version": ctx.project.version}
        if kind == "model":
            types = active_definition(session, ctx.project)["types"]
            nodes = [
                {
                    "id": t["key"],
                    "label": t["label"],
                    "type_key": t["key"],
                    "type_label": t["label"],
                    "kind": "type",
                    "properties": {"description": t.get("description", "")},
                    "visible_property_count": sum(
                        allowed_field(prop, ctx.role) for prop in t.get("properties", [])
                    ),
                }
                for t in types[:limit]
            ]
            ids = {x["id"] for x in nodes}
            edges = [
                {
                    "id": f"{t['key']}.{r['key']}",
                    "source": t["key"],
                    "target": r["target_type"],
                    "label": r["label"],
                    "type_key": r["key"],
                    "kind": "definition",
                    "cardinality": r["cardinality"],
                }
                for t in types
                if t["key"] in ids
                for r in t.get("relations", [])
                if r["target_type"] in ids
            ]
            return {
                **graph_baseline,
                "nodes": nodes,
                "edges": edges[:1000],
                "truncated": len(types) > limit or len(edges) > 1000,
            }
        if kind == "knowledge":
            from ontology.content_access.service import content_context
            from ontology.knowledge.api import knowledge_graph

            ctx = content_context(session, request, user, project_id)
            return {**knowledge_graph(session, ctx, limit), **graph_baseline}
        if kind != "instances":
            raise DomainError(422, "invalid_graph", "不支持的图谱类型")
        rows = session.scalars(
            select(Entity)
            .where(
                Entity.project_id == project_id,
                Entity.tenant_id == user.tenant_id,
                Entity.deleted.is_(False),
                Entity.data_status == "active",
            )
            .order_by(Entity.id)
            .limit(limit + 1)
        ).all()
        from ontology.objects.graph import graph_slice

        ids = [obj.id for obj in rows[:limit]]
        rels = session.scalars(
            select(Relation)
            .where(
                Relation.tenant_id == user.tenant_id,
                Relation.project_id == project_id,
                Relation.source_id.in_(ids),
                Relation.target_id.in_(ids),
            )
            .order_by(Relation.id)
            .limit(1001)
        ).all()
        return graph_slice(session, ctx, rows[:limit], rels, truncated=len(rows) > limit)


@router.get("/audit")
async def audit_events(
    request: Request,
    project_id: str,
    limit: int = Query(100, ge=1, le=200),
    offset: int = Query(0, ge=0),
    user=Depends(authenticated),
):
    from ontology.content_access.service import audit_condition, content_context, refresh_project

    with request.app.state.db.transaction(user.tenant_id) as session:
        access(session, user, project_id, "audit:read")
    await refresh_project(request, user, project_id)
    with request.app.state.db.transaction(user.tenant_id) as session:
        ctx = content_context(session, request, user, project_id, permission="audit:read")
        conditions = [Audit.tenant_id == user.tenant_id, Audit.project_id == project_id, audit_condition(ctx)]
        rows = session.scalars(
            select(Audit).where(*conditions).order_by(Audit.created_at.desc()).offset(offset).limit(limit)
        ).all()
        return {
            "items": [row_dict(x) for x in rows],
            "total": session.scalar(select(func.count()).select_from(Audit).where(*conditions)),
        }
