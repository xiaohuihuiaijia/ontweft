from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy import func, select, update

from ontology.content_access.service import (
    content_context,
    document_condition,
    inherit_dependencies,
    knowledge_condition,
    prepare_content_access,
    require_document,
    require_knowledge,
)
from ontology.core.domain import compare_and_swap, row_dict, scoped_record
from ontology.core.errors import DomainError, conflict, missing
from ontology.core.schemas import RevisionInput
from ontology.documents.history import (
    canonical_references,
)
from ontology.documents.storage_models import Document
from ontology.identity.security import audit, authenticated
from ontology.knowledge.models import KnowledgePage, KnowledgeRevision
from ontology.knowledge.references import validate_references
from ontology.knowledge.schemas import KnowledgeInput, KnowledgeUpdate
from ontology.search.content.index import enqueue_content

router = APIRouter(dependencies=[Depends(prepare_content_access)])
KNOWLEDGE_GRAPH_EDGE_LIMIT = 1000


def save_knowledge_revision(session, page, user):
    inherit_dependencies(session, page)
    session.add(
        KnowledgeRevision(
            tenant_id=user.tenant_id,
            project_id=page.project_id,
            page_id=page.id,
            revision=page.revision,
            title=page.title,
            body=page.body,
            references=page.references,
            author_id=user.id,
        )
    )


@router.get("/knowledge")
def pages(
    request: Request,
    project_id: str,
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    user=Depends(authenticated),
):
    with request.app.state.db.transaction(user.tenant_id) as session:
        ctx = content_context(session, request, user, project_id)
        conditions = [KnowledgePage.tenant_id == user.tenant_id, KnowledgePage.project_id == project_id]
        revision = (
            KnowledgePage.revision
            if {"knowledge:write", "knowledge:publish"} & ctx.permissions
            else KnowledgePage.published_revision
        )
        conditions.append(knowledge_condition(ctx, revision=revision))
        if "knowledge:write" not in ctx.permissions and "knowledge:publish" not in ctx.permissions:
            conditions.append(KnowledgePage.published_revision.is_not(None))
        can_edit = bool({"knowledge:write", "knowledge:publish"} & ctx.permissions)
        columns = [
            getattr(KnowledgeRevision if not can_edit and column.name in {"title", "references", "revision"}
                    else KnowledgePage, column.name).label(column.name)
            for column in KnowledgePage.__table__.columns if column.name != "body"
        ]
        query = select(*columns).select_from(KnowledgePage)
        if not can_edit:
            query = query.join(KnowledgeRevision, (
                (KnowledgeRevision.page_id == KnowledgePage.id)
                & (KnowledgeRevision.tenant_id == user.tenant_id)
                & (KnowledgeRevision.revision == KnowledgePage.published_revision)
            ))
        rows = session.execute(
            query
            .where(*conditions)
            .order_by(KnowledgePage.created_at.desc(), KnowledgePage.id.desc())
            .offset(offset)
            .limit(limit)
        ).mappings().all()
        return {
            "items": [dict(row) for row in rows],
            "total": session.scalar(select(func.count()).select_from(KnowledgePage).where(*conditions)),
        }


def page_view(session, row, ctx, summary=False):
    result = row_dict(row)
    if "knowledge:write" not in ctx.permissions and "knowledge:publish" not in ctx.permissions:
        if not row.published_revision:
            missing()
        revision = session.scalar(
            select(KnowledgeRevision).where(
                KnowledgeRevision.page_id == row.id, KnowledgeRevision.revision == row.published_revision
            )
        )
        result.update(
            title=revision.title,
            body=revision.body,
            references=revision.references,
            revision=revision.revision,
        )
    if summary:
        result.pop("body", None)
    return result


@router.post("/knowledge", status_code=201)
def create_page(data: KnowledgeInput, request: Request, user=Depends(authenticated)):
    with request.app.state.db.transaction(user.tenant_id) as session:
        ctx = content_context(session, request, user, data.project_id, permission="knowledge:write")
        references = validate_references(session, ctx, data.references)
        row = KnowledgePage(
            tenant_id=user.tenant_id,
            project_id=data.project_id,
            title=data.title,
            body=data.body,
            references=references,
            author_id=user.id,
        )
        session.add(row)
        session.flush()
        save_knowledge_revision(session, row, user)
        audit(session, user, data.project_id, "knowledge.created", row.id)
        return row_dict(row)


@router.get("/knowledge/{page_id}")
def get_page(page_id: str, request: Request, user=Depends(authenticated)):
    with request.app.state.db.transaction(user.tenant_id) as session:
        row = scoped_record(session, KnowledgePage, page_id, user)
        ctx = content_context(session, request, user, row.project_id)
        revision = (
            row.revision
            if {"knowledge:write", "knowledge:publish"} & ctx.permissions
            else row.published_revision
        )
        require_knowledge(session, ctx, row, revision=revision)
        return page_view(session, row, ctx)


@router.patch("/knowledge/{page_id}")
def update_page(page_id: str, data: KnowledgeUpdate, request: Request, user=Depends(authenticated)):
    with request.app.state.db.transaction(user.tenant_id) as session:
        row = scoped_record(session, KnowledgePage, page_id, user)
        ctx = content_context(session, request, user, row.project_id, permission="knowledge:write")
        require_knowledge(session, ctx, row)
        references = validate_references(session, ctx, data.references)
        compare_and_swap(
            session,
            row,
            data.expected_revision,
            {"title": data.title, "body": data.body, "references": references},
        )
        save_knowledge_revision(session, row, user)
        audit(session, user, row.project_id, "knowledge.edited", row.id, {"revision": row.revision})
        return row_dict(row)


@router.post("/knowledge/{page_id}/publish")
def publish_page(page_id: str, data: RevisionInput, request: Request, user=Depends(authenticated)):
    with request.app.state.db.transaction(user.tenant_id) as session:
        row = scoped_record(session, KnowledgePage, page_id, user)
        ctx = content_context(session, request, user, row.project_id, permission="knowledge:publish")
        require_knowledge(session, ctx, row)
        if row.revision != data.expected_revision:
            conflict()
        for reference in row.references:
            require_document(session, ctx, reference.get("document_id"))
        canonical_references(session, ctx, row.references, capture=False)
        if not row.body.strip():
            raise DomainError(422, "empty_page", "空正文不可发布")
        result = session.execute(
            update(KnowledgePage)
            .where(KnowledgePage.id == row.id, KnowledgePage.revision == data.expected_revision)
            .values(published_revision=row.revision)
        )
        if result.rowcount != 1:
            conflict()
        session.refresh(row)
        enqueue_content(session, row, "knowledge")
        audit(session, user, row.project_id, "knowledge.published", row.id, {"revision": row.revision})
        return row_dict(row)


@router.get("/knowledge/{page_id}/history")
def knowledge_history(page_id: str, request: Request, user=Depends(authenticated)):
    with request.app.state.db.transaction(user.tenant_id) as session:
        row = scoped_record(session, KnowledgePage, page_id, user)
        ctx = content_context(session, request, user, row.project_id, permission="knowledge:write")
        rows = session.scalars(
            select(KnowledgeRevision)
            .where(
                KnowledgeRevision.page_id == page_id,
                KnowledgeRevision.tenant_id == user.tenant_id,
                knowledge_condition(ctx, KnowledgeRevision.page_id, KnowledgeRevision.revision),
            )
            .order_by(KnowledgeRevision.revision.desc())
        ).all()
        return {"items": [row_dict(x) for x in rows], "total": len(rows)}


def knowledge_graph(session, ctx, limit):
    pages = session.execute(
        select(KnowledgePage.id, KnowledgeRevision.title, KnowledgeRevision.references)
        .join(
            KnowledgeRevision,
            (KnowledgeRevision.page_id == KnowledgePage.id)
            & (KnowledgeRevision.revision == KnowledgePage.published_revision),
        )
        .where(
            KnowledgePage.project_id == ctx.project.id,
            KnowledgePage.tenant_id == ctx.user.tenant_id,
            KnowledgePage.published_revision.is_not(None),
            knowledge_condition(ctx, revision=KnowledgePage.published_revision),
        )
        .order_by(KnowledgePage.id)
        .limit(limit + 1)
    ).all()
    nodes, edges = [], []
    ids = set()
    truncated = len(pages) > limit
    for page in pages[:limit]:
        nodes.append(
            {
                "id": page.id,
                "label": page.title,
                "kind": "knowledge",
                "type_key": "knowledge",
                "properties": {},
            }
        )
        ids.add(page.id)
    document_ids = sorted({ref["document_id"] for page in pages[:limit] for ref in page.references})
    documents = {}
    # Shared citations load each document once and never pull its potentially
    # large parsed IR into a graph response. Bound IN lists for SQLite and PG.
    for start in range(0, len(document_ids), 500):
        documents.update(
            (doc.id, doc)
            for doc in session.execute(
                select(Document.id, Document.filename, Document.revision).where(
                    Document.id.in_(document_ids[start : start + 500]),
                    Document.project_id == ctx.project.id,
                    Document.tenant_id == ctx.user.tenant_id,
                    document_condition(ctx),
                )
            )
        )
    for page in pages[:limit]:
        for ref in page.references:
            doc = documents.get(ref["document_id"])
            if doc and doc.id not in ids:
                if len(nodes) >= limit:
                    truncated = True
                    continue
                nodes.append(
                    {
                        "id": doc.id,
                        "label": doc.filename,
                        "kind": "document",
                        "type_key": "document",
                        "properties": {"revision": doc.revision},
                    }
                )
                ids.add(doc.id)
            if doc and doc.id in ids:
                if len(edges) >= KNOWLEDGE_GRAPH_EDGE_LIMIT:
                    truncated = True
                    continue
                edges.append(
                    {
                        "id": f"{page.id}:{doc.id}:{len(edges)}",
                        "source": page.id,
                        "target": doc.id,
                        "label": "引用",
                        "type_key": "citation",
                        "kind": "citation",
                    }
                )
    return {"nodes": nodes, "edges": edges, "truncated": truncated}
