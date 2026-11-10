"""Authorized all-term object retrieval from current field-level projections."""

from contextlib import contextmanager
from time import monotonic

from fastapi import APIRouter, Depends, Request
from pydantic import Field
from sqlalchemy import and_, event, exists, func, or_, select, text
from sqlalchemy.exc import DBAPIError

from ontology.core.domain import active_definition, entity_view
from ontology.core.errors import DomainError, conflict
from ontology.core.schemas import StrictModel
from ontology.identity.security import access, allowed_field, audit, authenticated
from ontology.objects.models import Entity
from ontology.search.objects.index import current_state, index_status, reset_project, scoped
from ontology.search.objects.models import ObjectSearchDocument, ObjectSearchField
from ontology.search.tokens import TOKENIZER_VERSION, SearchBudgetError, query_tokens

router = APIRouter()


@contextmanager
def read_transaction(db, tenant_id):
    try:
        with db.transaction(tenant_id) as session:
            if session.bind.dialect.name == "postgresql":
                session.execute(text("SET LOCAL statement_timeout = '3000ms'"))
                session.execute(text("SET LOCAL lock_timeout = '1000ms'"))
                deadline = monotonic() + 3

                def remaining_budget(_):
                    remaining = int((deadline - monotonic()) * 1000)
                    if remaining <= 0:
                        raise DomainError(
                            503, "search_time_budget", "全文查询超过总时间预算，请缩小范围后重试"
                        )
                    # Direct connection execution avoids recursively entering
                    # the ORM hook. Every statement shares one total deadline.
                    session.connection().execute(
                        text("SELECT set_config('statement_timeout', :timeout, true)"),
                        {"timeout": f"{remaining}ms"},
                    )

                event.listen(session, "do_orm_execute", remaining_budget)
            yield session
    except DBAPIError as exc:
        diagnostic = exc.orig.args[0] if getattr(exc.orig, "args", ()) else None
        state = getattr(exc.orig, "sqlstate", None) or (
            diagnostic.get("C") if isinstance(diagnostic, dict) else None
        )
        if state in {"57014", "55P03"}:
            raise DomainError(
                503, "search_time_budget", "全文查询超过时间或锁等待预算，请缩小范围后重试"
            ) from None
        raise


class SearchBaseline(StrictModel):
    project_id: str
    expected_release_id: str | None
    expected_model_version: int = Field(ge=0)


class SearchInput(SearchBaseline):
    q: str = Field(min_length=1, max_length=200)
    type_key: str | None = None
    limit: int = Field(default=20, ge=1, le=50)
    offset: int = Field(default=0, ge=0, le=1000)


def read_context(session, user, project_id):
    # A response uses one model/membership/object baseline through count and
    # pagination. Current authorization is read after waiting for the lock.
    return access(session, user, project_id, read_lock=True)


def baseline(ctx, data):
    if (
        data.expected_release_id != ctx.project.active_release_id
        or data.expected_model_version != ctx.project.version
    ):
        conflict("检索模型基线已改变，请刷新模型后重新执行查询")


def term_match(session, term):
    # Equality can use the scoped B-tree under forced RLS. PG's non-leakproof
    # @@ / to_tsvector expression caused a full tenant scan in measured plans.
    # Terms are already normalized by the shared versioned tokenizer.
    return ObjectSearchField.token_text == " " + term + " "


@router.get("/search/status")
def status(request: Request, project_id: str, user=Depends(authenticated)):
    with read_transaction(request.app.state.db, user.tenant_id) as session:
        ctx = read_context(session, user, project_id)
        return index_status(session, ctx.project)


@router.post("/search/rebuild", status_code=202)
def rebuild(data: SearchBaseline, request: Request, user=Depends(authenticated)):
    with request.app.state.db.transaction(user.tenant_id) as session:
        ctx = access(session, user, data.project_id, "sources:write")
        session.refresh(ctx.project)
        baseline(ctx, data)
        if not ctx.project.active_release_id:
            conflict("请先发布本体模型")
        state = reset_project(session, ctx.project)
        audit(
            session,
            user,
            ctx.project.id,
            "search.rebuild.requested",
            state.id,
            {"generation": state.generation},
        )
        return index_status(session, ctx.project)


@router.post("/search/objects")
def search(data: SearchInput, request: Request, user=Depends(authenticated)):
    with read_transaction(request.app.state.db, user.tenant_id) as session:
        ctx = read_context(session, user, data.project_id)
        baseline(ctx, data)
        definitions = active_definition(session, ctx.project)["types"]
        if data.type_key and not any(t["key"] == data.type_key for t in definitions):
            raise DomainError(422, "unknown_type", "对象类型尚未发布")
        try:
            terms = query_tokens(data.q)
        except SearchBudgetError as exc:
            raise DomainError(422, "search_query_budget", str(exc)) from None
        state = current_state(session, ctx.project)
        result = {
            "items": [],
            "total": 0,
            "limit": data.limit,
            "offset": data.offset,
            "query": {"text": data.q, "terms": terms, "mode": "all_terms"},
            "release_id": ctx.project.active_release_id,
            "model_version": ctx.project.version,
            "index": index_status(session, ctx.project, state=state),
            "matches": {},
        }
        if state is None:
            return result
        visible = {
            definition["key"]: [
                p["key"]
                for p in definition["properties"]
                if p["data_type"] in {"string", "enum"} and allowed_field(p, ctx.role)
            ]
            for definition in definitions
            if not data.type_key or definition["key"] == data.type_key
        }
        access_terms = [
            and_(Entity.type_key == key, ObjectSearchField.field_key.in_(fields))
            for key, fields in visible.items()
            if fields
        ]
        if not access_terms:
            return result
        field_conditions = [
            *scoped(ObjectSearchField, ctx.project),
            ObjectSearchField.entity_id == Entity.id,
            ObjectSearchField.generation == state.generation,
            or_(*access_terms),
        ]
        conditions = [
            *scoped(Entity, ctx.project),
            *scoped(ObjectSearchDocument, ctx.project),
            Entity.deleted.is_(False),
            Entity.data_status == "active",
            ObjectSearchDocument.entity_revision == Entity.revision,
            ObjectSearchDocument.release_id == ctx.project.active_release_id,
            ObjectSearchDocument.generation == state.generation,
            ObjectSearchDocument.tokenizer_version == TOKENIZER_VERSION,
            ObjectSearchDocument.status == "ready",
        ]
        # Each term may occur in a different authorized field posting. Hidden
        # fields never contribute to membership, counts, ordering or snippets.
        for term in terms:
            conditions.append(
                exists(
                    select(ObjectSearchField.id)
                    .where(
                        *field_conditions,
                        term_match(session, term),
                    )
                    .correlate(Entity)
                )
            )
        query = (
            select(Entity)
            .join(ObjectSearchDocument, ObjectSearchDocument.entity_id == Entity.id)
            .where(*conditions)
        )
        result["total"] = session.scalar(select(func.count()).select_from(query.subquery()))
        entities = session.scalars(
            query.order_by(Entity.updated_at.desc(), Entity.id).offset(data.offset).limit(data.limit)
        ).all()
        by_type = {definition["key"]: definition for definition in definitions}
        result["items"] = [
            entity_view(session, entity, ctx, definition=by_type[entity.type_key]) for entity in entities
        ]
        result["matches"] = {entity.id: {"fields": []} for entity in entities}
        if entities:
            fields = session.execute(
                select(ObjectSearchField.entity_id, ObjectSearchField.field_key)
                .join(Entity, ObjectSearchField.entity_id == Entity.id)
                .where(
                    *scoped(ObjectSearchField, ctx.project),
                    ObjectSearchField.entity_id.in_([entity.id for entity in entities]),
                    ObjectSearchField.generation == state.generation,
                    or_(*access_terms),
                    or_(*(term_match(session, term) for term in terms)),
                )
                .distinct()
                .order_by(ObjectSearchField.entity_id, ObjectSearchField.field_key)
            ).all()
            for entity_id, field in fields:
                result["matches"][entity_id]["fields"].append(field)
        return result
