import operator
from decimal import Decimal
from typing import Literal

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy import Numeric, cast, func, or_, select

from ontology.audit.models import Audit
from ontology.content_access.evidence import filter_evidence
from ontology.content_access.service import content_context, prepare_content_access
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
from ontology.objects.models import Entity, EntityRevision, Relation, RelationRevision
from ontology.objects.relation_history import (
    prepare_relation_properties,
    record_relation_revision,
    relation_values_equal,
    relation_views,
)
from ontology.objects.schemas import (
    ModelRevisionInput,
    ObjectInput,
    ObjectQuery,
    ObjectUpdate,
    RelationInput,
    RelationUpdate,
)
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
    filter_evidence(session, ctx, [ref for obj in objects for ref in obj.evidence])
    return {
        "items": [entity_view(session, obj, ctx, definition=type_def) for obj in objects],
        "total": total,
        "release_id": ctx.project.active_release_id,
        "version": ctx.project.version,
    }


@router.post("/query", dependencies=[Depends(prepare_content_access)])
def query(data: ObjectQuery, request: Request, user=Depends(authenticated)):
    with request.app.state.db.transaction(user.tenant_id) as session:
        ctx = content_context(session, request, user, data.project_id)
        return query_objects(session, ctx, data)


@router.get("/objects", dependencies=[Depends(prepare_content_access)])
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
        ctx = content_context(session, request, user, project_id)
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
        by_type = {definition["key"]: definition for definition in definitions}
        filter_evidence(session, ctx, [ref for row in rows for ref in row.evidence])
        return {
            "items": [entity_view(session, x, ctx, definition=by_type.get(x.type_key)) for x in rows],
            "total": total,
            "release_id": ctx.project.active_release_id,
        }


@router.post("/objects", status_code=201, dependencies=[Depends(prepare_content_access)])
def create_object(data: ObjectInput, request: Request, user=Depends(authenticated)):
    with request.app.state.db.transaction(user.tenant_id) as session:
        ctx = access(session, user, data.project_id, "objects:write")
        ensure_model_baseline(session, ctx, data)
        ctx = content_context(session, request, user, data.project_id)
        definition = type_definition(session, ctx.project, data.type_key)
        ensure_writable(definition, data.properties, ctx.role)
        values = validate_properties(definition, data.properties)
        evidence = []
        if data.evidence:
            from ontology.knowledge.references import validate_references

            evidence = validate_references(session, ctx, data.evidence)
        obj = Entity(
            tenant_id=user.tenant_id,
            project_id=data.project_id,
            type_key=data.type_key,
            properties=values,
            evidence=evidence,
            release_id=ctx.project.active_release_id,
        )
        session.add(obj)
        session.flush()
        save_entity_revision(session, obj, user)
        audit(session, user, data.project_id, "object.created", obj.id)
        return entity_view(session, obj, ctx)


@router.get("/objects/{object_id}", dependencies=[Depends(prepare_content_access)])
def get_object(object_id: str, request: Request, user=Depends(authenticated)):
    with request.app.state.db.transaction(user.tenant_id) as session:
        obj = scoped_record(session, Entity, object_id, user)
        ctx = content_context(session, request, user, obj.project_id)
        if obj.deleted:
            missing()
        return entity_view(session, obj, ctx)


@router.patch("/objects/{object_id}", dependencies=[Depends(prepare_content_access)])
def update_object(object_id: str, data: ObjectUpdate, request: Request, user=Depends(authenticated)):
    with request.app.state.db.transaction(user.tenant_id) as session:
        obj = scoped_record(session, Entity, object_id, user)
        ctx = access(session, user, obj.project_id, "objects:write")
        ensure_model_baseline(session, ctx, data)
        ctx = content_context(session, request, user, obj.project_id)
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


@router.get("/objects/{object_id}/history", dependencies=[Depends(prepare_content_access)])
def object_history(object_id: str, request: Request, user=Depends(authenticated)):
    with request.app.state.db.transaction(user.tenant_id) as session:
        obj = scoped_record(session, Entity, object_id, user)
        ctx = content_context(session, request, user, obj.project_id)
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
        filter_evidence(session, ctx, [ref for row in rows for ref in row.evidence])
        return {
            "items": [
                {
                    **row_dict(x),
                    "properties": wire_value({k: v for k, v in x.properties.items() if k in visible}),
                    "evidence": filter_evidence(session, ctx, visible_evidence(x.evidence, visible)),
                }
                for x in rows
            ],
            "total": len(rows),
        }


@router.get("/relations", dependencies=[Depends(prepare_content_access)])
def relations(
    request: Request,
    project_id: str,
    object_id: str | None = None,
    limit: int = Query(200, ge=1, le=1000),
    offset: int = Query(0, ge=0),
    user=Depends(authenticated),
):
    with request.app.state.db.transaction(user.tenant_id) as session:
        ctx = content_context(session, request, user, project_id)
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
        rows = session.scalars(
            select(Relation).where(*conditions).order_by(Relation.id).offset(offset).limit(limit)
        ).all()
        return {
            "items": relation_views(session, ctx, rows),
            "total": session.scalar(select(func.count()).select_from(Relation).where(*conditions)),
            "limit": limit,
            "offset": offset,
        }


@router.post("/relations", status_code=201, dependencies=[Depends(prepare_content_access)])
def create_relation(data: RelationInput, request: Request, user=Depends(authenticated)):
    with request.app.state.db.transaction(user.tenant_id, write=True) as session:
        ctx = access(session, user, data.project_id, "objects:write")
        ensure_model_baseline(session, ctx, data)
        ctx = content_context(session, request, user, data.project_id)
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
        values = prepare_relation_properties(relation, data.properties, ctx)
        evidence = validate_relation_evidence(session, ctx, data.evidence)
        existing = session.scalar(
            select(Relation).where(
                Relation.tenant_id == user.tenant_id,
                Relation.project_id == data.project_id,
                Relation.source_id == source.id,
                Relation.target_id == target.id,
                Relation.type_key == data.type_key,
            )
        )
        if existing:
            fields = {prop["key"]: prop for prop in relation.get("properties", [])}
            if any(
                key in existing.properties
                and not relation_values_equal(fields[key], existing.properties[key], value)
                for key, value in values.items()
            ):
                conflict("关系已有不同属性，请打开关系详情核对后修改")
            updated = {**values, **existing.properties}
            refs = existing.evidence + [ref for ref in evidence if ref not in existing.evidence]
            if not existing.manual or updated != existing.properties or refs != existing.evidence:
                record_relation_revision(
                    session,
                    existing,
                    actor_id=user.id,
                    model_release_id=ctx.project.active_release_id,
                    reason="修改前状态",
                    operation="baseline",
                )
                existing.manual = True
                existing.properties, existing.evidence = updated, refs
                existing.revision += 1
                from ontology.objects.constraints import touch_entity

                touch_entity(session, source)
                record_relation_revision(
                    session,
                    existing,
                    actor_id=user.id,
                    model_release_id=ctx.project.active_release_id,
                    reason="确认人工支持",
                    operation="updated",
                )
                audit(session, user, data.project_id, "relation.manual.retained", existing.id)
            return relation_views(session, ctx, [existing])[0]
        if relation["cardinality"] == "one" and session.scalar(
            select(Relation.id).where(
                Relation.project_id == data.project_id,
                Relation.source_id == source.id,
                Relation.type_key == data.type_key,
            )
        ):
            conflict("该关系只允许一个目标")
        row = Relation(
            tenant_id=user.tenant_id,
            project_id=data.project_id,
            type_key=data.type_key,
            source_id=source.id,
            target_id=target.id,
            properties=values,
            evidence=evidence,
        )
        session.add(row)
        from ontology.objects.constraints import touch_entity

        touch_entity(session, source)
        session.flush()
        record_relation_revision(
            session,
            row,
            actor_id=user.id,
            model_release_id=ctx.project.active_release_id,
            reason="人工创建关系",
            operation="created",
        )
        audit(session, user, data.project_id, "relation.created", row.id)
        return relation_views(session, ctx, [row])[0]


@router.get("/relations/{relation_id}", dependencies=[Depends(prepare_content_access)])
def relation_detail(relation_id: str, request: Request, user=Depends(authenticated)):
    with request.app.state.db.transaction(user.tenant_id) as session:
        row = scoped_record(session, Relation, relation_id, user)
        ctx = content_context(session, request, user, row.project_id)
        for identifier in (row.source_id, row.target_id):
            endpoint = scoped_record(session, Entity, identifier, user)
            if endpoint.project_id != row.project_id or endpoint.deleted:
                missing()
            require_active_entity(endpoint)
        return relation_views(session, ctx, [row])[0]


@router.patch("/relations/{relation_id}", dependencies=[Depends(prepare_content_access)])
def update_relation(relation_id: str, data: RelationUpdate, request: Request, user=Depends(authenticated)):
    with request.app.state.db.transaction(user.tenant_id, write=True) as session:
        row = scoped_record(session, Relation, relation_id, user)
        ctx = access(session, user, row.project_id, "objects:write")
        ensure_model_baseline(session, ctx, data)
        ctx = content_context(session, request, user, row.project_id)
        session.refresh(row)
        if row.revision != data.expected_revision:
            conflict()
        source = scoped_record(session, Entity, row.source_id, user)
        target = scoped_record(session, Entity, row.target_id, user)
        for endpoint in (source, target):
            if endpoint.project_id != row.project_id or endpoint.deleted:
                missing()
            require_active_entity(endpoint)
        definition = type_definition(session, ctx.project, source.type_key)
        relation = next(
            (item for item in definition.get("relations", []) if item["key"] == row.type_key), None
        )
        if not relation or relation["target_type"] != target.type_key:
            conflict("关系定义已改变，请先核对当前模型")
        values = prepare_relation_properties(
            relation, data.properties, ctx, existing=row.properties, remove_properties=data.remove_properties
        )
        evidence = validate_relation_evidence(session, ctx, data.evidence)
        refs = row.evidence + [ref for ref in evidence if ref not in row.evidence]
        if values == row.properties and refs == row.evidence:
            return relation_views(session, ctx, [row])[0]
        record_relation_revision(
            session,
            row,
            actor_id=user.id,
            model_release_id=ctx.project.active_release_id,
            reason="修改前状态",
            operation="baseline",
        )
        compare_and_swap(session, row, data.expected_revision, {"properties": values, "evidence": refs})
        from ontology.objects.constraints import touch_entity

        touch_entity(session, source)
        record_relation_revision(
            session,
            row,
            actor_id=user.id,
            model_release_id=ctx.project.active_release_id,
            reason=data.reason,
            operation="updated",
        )
        audit(session, user, row.project_id, "relation.edited", row.id, {"revision": row.revision})
        return relation_views(session, ctx, [row])[0]


def validate_relation_evidence(session, ctx, references):
    from ontology.knowledge.references import validate_references

    pages = [ref for ref in references if "page" in ref]
    canonical = [ref for ref in references if ref not in pages]
    result = validate_references(session, ctx, canonical) if canonical else []
    if pages:
        from ontology.intelligence.proposal_evidence import validate_page_evidence

        result.extend(validate_page_evidence(session, ctx, pages))
    return result


@router.get("/relations/{relation_id}/history")
async def relation_history(
    relation_id: str,
    request: Request,
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    user=Depends(authenticated),
):
    from ontology.content_access.service import refresh_project

    with request.app.state.db.transaction(user.tenant_id) as session:
        project_id = session.scalar(
            select(Relation.project_id).where(
                Relation.id == relation_id,
                Relation.tenant_id == user.tenant_id,
            )
        ) or session.scalar(
            select(RelationRevision.project_id)
            .where(
                RelationRevision.relation_id == relation_id,
                RelationRevision.tenant_id == user.tenant_id,
            )
            .limit(1)
        )
        if not project_id:
            missing()
        access(session, user, project_id)
    await refresh_project(request, user, project_id)
    with request.app.state.db.transaction(user.tenant_id) as session:
        ctx = content_context(session, request, user, project_id)
        conditions = (
            RelationRevision.relation_id == relation_id,
            RelationRevision.tenant_id == user.tenant_id,
            RelationRevision.project_id == project_id,
        )
        rows = session.scalars(
            select(RelationRevision)
            .where(*conditions)
            .order_by(RelationRevision.revision.desc())
            .offset(offset)
            .limit(limit)
        ).all()
        return {
            "items": relation_views(session, ctx, rows, history=True),
            "total": session.scalar(select(func.count()).select_from(RelationRevision).where(*conditions)),
            "limit": limit,
            "offset": offset,
        }


@router.delete("/relations/{relation_id}")
def delete_relation(
    relation_id: str, data: ModelRevisionInput, request: Request, user=Depends(authenticated)
):
    with request.app.state.db.transaction(user.tenant_id, write=True) as session:
        row = scoped_record(session, Relation, relation_id, user)
        ctx = access(session, user, row.project_id, "objects:write")
        ensure_model_baseline(session, ctx, data)
        row = session.scalar(
            select(Relation)
            .where(Relation.id == relation_id, Relation.tenant_id == user.tenant_id)
            .execution_options(populate_existing=True)
        )
        if row is None:
            missing()
        if row.revision != data.expected_revision:
            conflict()
        from ontology.objects.rule_models import RelationContribution

        supported = session.scalar(
            select(RelationContribution.id).where(RelationContribution.relation_id == row.id).limit(1)
        )
        from ontology.intelligence.proposal_models import DocumentRelationContribution

        document_supported = session.scalar(
            select(DocumentRelationContribution.id)
            .where(DocumentRelationContribution.relation_id == row.id)
            .limit(1)
        )
        if supported or document_supported:
            if not row.manual:
                conflict("关系仍有来源或持续规则贡献，请先处理对应来源")
            record_relation_revision(
                session,
                row,
                actor_id=user.id,
                model_release_id=ctx.project.active_release_id,
                reason="撤回人工支持前",
                operation="baseline",
            )
            row.manual = False
            row.revision += 1
            from ontology.objects.constraints import touch_entity

            touch_entity(session, session.get(Entity, row.source_id))
            record_relation_revision(
                session,
                row,
                actor_id=user.id,
                model_release_id=ctx.project.active_release_id,
                reason="撤回人工支持，保留来源关系",
                operation="updated",
            )
            audit(session, user, row.project_id, "relation.manual.removed", row.id)
            return {
                "ok": True,
                "retained_by_rules": bool(supported),
                "retained_by_document": bool(document_supported),
            }
        from ontology.objects.constraints import touch_entity

        touch_entity(session, session.get(Entity, row.source_id))
        source = session.get(Entity, row.source_id)
        record_relation_revision(
            session,
            row,
            actor_id=user.id,
            model_release_id=ctx.project.active_release_id,
            reason="删除前状态",
            operation="baseline",
        )
        row.revision += 1
        record_relation_revision(
            session,
            row,
            actor_id=user.id,
            model_release_id=ctx.project.active_release_id,
            reason="删除无其他支持的人工关系",
            operation="deleted",
        )
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
