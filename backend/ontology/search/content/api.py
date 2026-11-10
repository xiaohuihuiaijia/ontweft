"""Current-authorized content search and exact, version-bound evidence reads."""

from typing import Literal
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Request
from pydantic import Field
from sqlalchemy import and_, exists, func, literal_column, or_, select

from ontology.content_access.service import (
    content_context,
    document_condition,
    knowledge_condition,
    prepare_content_access,
)
from ontology.core.domain import wire_value
from ontology.core.errors import DomainError, conflict
from ontology.core.schemas import StrictModel
from ontology.documents.history import reference_status
from ontology.documents.models import DocumentError
from ontology.documents.storage_models import Document
from ontology.identity.security import audit, authenticated
from ontology.knowledge.models import KnowledgePage, KnowledgeRevision
from ontology.search.content.index import KINDS, index_status, reset_kind
from ontology.search.content.models import ContentSearchChunk, ContentSearchDocument, ContentSearchState
from ontology.search.content.units import (
    CHUNKER_VERSION,
    ir_fingerprint,
    resolve_document_anchor,
    resolve_knowledge_anchor,
)
from ontology.search.objects.api import read_transaction
from ontology.search.objects.index import scoped
from ontology.search.tokens import TOKENIZER_VERSION, SearchBudgetError, query_tokens

router = APIRouter(dependencies=[Depends(prepare_content_access)])
ContentKind = Literal["all", "document", "knowledge"]
EvidenceMode = Literal["original_evidence", "comprehensive_knowledge"]


class ContentQuery(StrictModel):
    project_id: str
    q: str = Field(min_length=1, max_length=200)
    kind: ContentKind = "all"
    evidence_mode: EvidenceMode = "comprehensive_knowledge"
    limit: int = Field(default=20, ge=1, le=50)
    offset: int = Field(default=0, ge=0, le=1000)


class ContentRebuild(StrictModel):
    project_id: str
    kind: ContentKind = "all"
    expected_generations: dict[str, str | None]


class EvidenceInput(StrictModel):
    project_id: str
    chunk_id: str
    expected_generation: str
    expected_fingerprint: str
    expected_revision: int = Field(ge=1)
    expected_parse_revision: int = Field(ge=0)
    evidence_mode: EvidenceMode = "comprehensive_knowledge"


def _candidates(project, ctx):
    doc = ContentSearchDocument
    state = ContentSearchState
    live_document = exists(
        select(Document.id).where(
            *scoped(Document, project),
            Document.id == doc.document_id,
            Document.state.in_(["ready", "partial"]),
            Document.revision == doc.source_revision,
            Document.parse_revision == doc.parse_revision,
            Document.ir_fingerprint == doc.source_fingerprint,
            document_condition(ctx),
        )
    )
    live_knowledge = exists(
        select(KnowledgePage.id)
        .join(
            KnowledgeRevision,
            KnowledgeRevision.page_id == KnowledgePage.id,
        )
        .where(
            *scoped(KnowledgePage, project),
            *scoped(KnowledgeRevision, project),
            KnowledgePage.id == doc.page_id,
            KnowledgePage.published_revision == doc.source_revision,
            KnowledgeRevision.id == doc.knowledge_revision_id,
            KnowledgeRevision.revision == KnowledgePage.published_revision,
            knowledge_condition(ctx, revision=KnowledgePage.published_revision),
        )
    )
    return (
        select(ContentSearchChunk, doc)
        .join(
            doc,
            ContentSearchChunk.receipt_id == doc.id,
        )
        .join(state, and_(state.project_id == doc.project_id, state.kind == doc.asset_kind))
        .where(
            *scoped(ContentSearchChunk, project),
            *scoped(doc, project),
            *scoped(state, project),
            doc.status.in_(["ready", "partial"]),
            doc.generation == state.generation,
            doc.tokenizer_version == TOKENIZER_VERSION,
            state.tokenizer_version == TOKENIZER_VERSION,
            doc.chunker_version == CHUNKER_VERSION,
            state.chunker_version == CHUNKER_VERSION,
            or_(
                and_(doc.asset_kind == "document", live_document),
                and_(doc.asset_kind == "knowledge", live_knowledge),
            ),
        )
    )


def _item(chunk, receipt):
    coverage = receipt.coverage
    return {
        "id": chunk.id,
        "kind": receipt.asset_kind,
        "asset_id": receipt.asset_id,
        "title": receipt.title,
        "source_revision": receipt.source_revision,
        "parse_revision": receipt.parse_revision,
        "source_fingerprint": receipt.source_fingerprint,
        "generation": receipt.generation,
        "excerpt": chunk.text,
        "anchor": chunk.anchor,
        "status": receipt.status,
        "complete": coverage.get("complete", False),
        "quality": coverage.get("quality"),
        "warnings": coverage.get("warnings", []),
        "pages": coverage.get("pages", []),
    }


def _term_match(session, term):
    if session.bind.dialect.name == "postgresql":
        config = literal_column("'simple'::regconfig")
        return func.to_tsvector(config, ContentSearchChunk.token_text).op("@@")(
            func.plainto_tsquery(config, term),
        )
    return ContentSearchChunk.token_text.contains(" " + term + " ", autoescape=True)


@router.get("/search/content/status")
def status(request: Request, project_id: str, user=Depends(authenticated)):
    with read_transaction(request.app.state.db, user.tenant_id) as session:
        ctx = content_context(session, request, user, project_id)
        return index_status(session, ctx.project, access_context=ctx)


@router.post("/search/content/rebuild", status_code=202)
def rebuild(data: ContentRebuild, request: Request, user=Depends(authenticated)):
    with request.app.state.db.transaction(user.tenant_id) as session:
        ctx = content_context(session, request, user, data.project_id, permission="sources:write")
        kinds = KINDS if data.kind == "all" else (data.kind,)
        before = index_status(session, ctx.project, access_context=ctx)
        if set(data.expected_generations) != set(kinds):
            raise DomainError(422, "invalid_baseline", "请为每类待重建内容提供当前索引代次")
        if any(before[kind]["generation"] != data.expected_generations[kind] for kind in kinds):
            conflict("索引代次已改变，请刷新索引状态后重试")
        for kind in kinds:
            state = reset_kind(session, ctx.project, kind)
            audit(
                session,
                user,
                ctx.project.id,
                "content_search.rebuild.requested",
                state.id,
                {"kind": kind, "generation": state.generation},
            )
        return index_status(session, ctx.project, access_context=ctx)


@router.post("/search/content")
def search(data: ContentQuery, request: Request, user=Depends(authenticated)):
    with read_transaction(request.app.state.db, user.tenant_id) as session:
        ctx = content_context(session, request, user, data.project_id)
        if data.evidence_mode == "original_evidence" and data.kind == "knowledge":
            raise DomainError(422, "evidence_mode", "原始证据模式不检索已编写的知识页")
        try:
            terms = query_tokens(data.q)
        except SearchBudgetError as exc:
            raise DomainError(422, "search_query_budget", str(exc)) from None
        query = _candidates(ctx.project, ctx)
        if data.kind != "all":
            query = query.where(ContentSearchDocument.asset_kind == data.kind)
        if data.evidence_mode == "original_evidence":
            query = query.where(ContentSearchDocument.asset_kind == "document")
        for term in terms:
            query = query.where(_term_match(session, term))
        total = session.scalar(select(func.count()).select_from(query.subquery()))
        rows = session.execute(
            query.order_by(
                ContentSearchDocument.asset_kind,
                ContentSearchDocument.asset_id,
                ContentSearchChunk.ordinal,
            )
            .offset(data.offset)
            .limit(data.limit)
        ).all()
        return {
            "items": [_item(chunk, receipt) for chunk, receipt in rows],
            "total": total,
            "limit": data.limit,
            "offset": data.offset,
            "query": {"text": data.q, "terms": terms, "mode": "all_terms_in_fragment"},
            "evidence_mode": data.evidence_mode,
            "index": index_status(session, ctx.project, access_context=ctx),
        }


def _reference_status(session, project, reference):
    return reference_status(session, project, reference)


@router.post("/search/content/evidence")
def evidence(data: EvidenceInput, request: Request, user=Depends(authenticated)):
    with read_transaction(request.app.state.db, user.tenant_id) as session:
        ctx = content_context(session, request, user, data.project_id)
        # An old chunk may have been replaced. Do not silently resolve to the
        # latest source, even if its text happens to be identical.
        old = session.scalar(
            select(ContentSearchChunk.id).where(
                *scoped(ContentSearchChunk, ctx.project),
                ContentSearchChunk.id == data.chunk_id,
            )
        )
        if old is None:
            conflict("证据片段已失效，请重新检索")
        row = session.execute(
            _candidates(ctx.project, ctx).where(ContentSearchChunk.id == data.chunk_id)
        ).first()
        if row is None:
            conflict("来源或解析版本已改变，请重新检索")
        chunk, receipt = row
        if data.evidence_mode == "original_evidence" and receipt.asset_kind != "document":
            raise DomainError(422, "evidence_mode", "原始证据模式不读取知识页")
        if (
            receipt.generation,
            receipt.source_fingerprint,
            receipt.source_revision,
            receipt.parse_revision,
        ) != (
            data.expected_generation,
            data.expected_fingerprint,
            data.expected_revision,
            data.expected_parse_revision,
        ):
            conflict("证据版本不匹配，请重新检索")
        result = {"item": _item(chunk, receipt), "coverage": receipt.coverage}
        try:
            if receipt.asset_kind == "document":
                doc = session.scalar(
                    select(Document).where(*scoped(Document, ctx.project), Document.id == receipt.document_id)
                )
                if ir_fingerprint(doc.ir) != receipt.source_fingerprint:
                    conflict("解析内容已改变，请重新解析并检索")
                result["unit"] = resolve_document_anchor(doc, chunk.anchor)
                result["download_url"] = f"/api/documents/{doc.id}/download?" + urlencode(
                    {
                        "expected_revision": doc.revision,
                        "expected_hash": doc.content_hash,
                    }
                )
            else:
                revision = session.scalar(
                    select(KnowledgeRevision).where(
                        *scoped(KnowledgeRevision, ctx.project),
                        KnowledgeRevision.id == receipt.knowledge_revision_id,
                    )
                )
                result["unit"] = resolve_knowledge_anchor(revision, chunk.anchor)
                result["references"] = [
                    _reference_status(session, ctx.project, ref) for ref in revision.references
                ]
        except DocumentError:
            conflict("原文锚点已失效，请重新解析或重新检索")
        if result["unit"]["chunk_text"] != chunk.text:
            conflict("索引片段与原文不一致，请重建索引")
        result["unit"].update({key: chunk.anchor[key] for key in ("unit_text_start", "unit_text_end")})
        result["unit"]["metadata"] = wire_value(result["unit"]["metadata"])
        return result
