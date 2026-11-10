"""Rebuildable, version-bound document and published knowledge full-text index.

The existing project transaction serializes publication, parsing and maintenance.
One asset per turn avoids a large document monopolizing an entire batch. No model
release, embedding service or language model is required for this projection.
"""

import logging

from sqlalchemy import and_, delete, exists, func, insert, or_, select, update

from ontology.core.models import now, uid
from ontology.documents.models import DocumentError
from ontology.documents.storage_models import Document
from ontology.identity.models import Project
from ontology.knowledge.models import KnowledgePage, KnowledgeRevision
from ontology.search.content.models import (
    ContentSearchChunk,
    ContentSearchDocument,
    ContentSearchRequest,
    ContentSearchState,
)
from ontology.search.content.units import (
    CHUNKER_VERSION,
    document_chunks,
    ir_fingerprint,
    knowledge_chunks,
    validate_document_ir,
)
from ontology.search.objects.index import scoped
from ontology.search.tokens import TOKENIZER_VERSION, SearchBudgetError, tokens

logger = logging.getLogger(__name__)
KINDS = ("document", "knowledge")


def enqueue_content(session, asset, kind):
    if kind not in KINDS:
        raise ValueError("Unsupported content kind")
    request = session.scalar(
        select(ContentSearchRequest).where(
            ContentSearchRequest.tenant_id == asset.tenant_id,
            ContentSearchRequest.project_id == asset.project_id,
            ContentSearchRequest.asset_kind == kind,
            ContentSearchRequest.asset_id == asset.id,
        )
    )
    if request is None:
        request = ContentSearchRequest(
            tenant_id=asset.tenant_id,
            project_id=asset.project_id,
            asset_kind=kind,
            asset_id=asset.id,
            document_id=asset.id if kind == "document" else None,
            page_id=asset.id if kind == "knowledge" else None,
        )
        session.add(request)
    request.updated_at, request.error = now(), None
    state = session.scalar(
        select(ContentSearchState).where(
            ContentSearchState.tenant_id == asset.tenant_id,
            ContentSearchState.project_id == asset.project_id,
            ContentSearchState.kind == kind,
        )
    )
    if state is None:
        # Establish a durable fence even before the first backfill. Otherwise
        # a rolled-back first attempt could mark a later retry as failed.
        project = session.get(Project, asset.project_id)
        state = reset_kind(session, project, kind)
    state.updated_at, state.error = now(), None


def current_state(session, project, kind):
    state = session.scalar(
        select(ContentSearchState).where(
            *scoped(ContentSearchState, project),
            ContentSearchState.kind == kind,
        )
    )
    if state and state.tokenizer_version == TOKENIZER_VERSION and state.chunker_version == CHUNKER_VERSION:
        return state
    return None


def reset_kind(session, project, kind):
    state = session.scalar(
        select(ContentSearchState).where(
            *scoped(ContentSearchState, project),
            ContentSearchState.kind == kind,
        )
    )
    if state is None:
        state = ContentSearchState(tenant_id=project.tenant_id, project_id=project.id, kind=kind)
        session.add(state)
    state.generation, state.tokenizer_version, state.chunker_version = (
        uid(),
        TOKENIZER_VERSION,
        CHUNKER_VERSION,
    )
    state.backfill_cursor, state.backfill_complete, state.error = "", False, None
    state.updated_at = now()
    session.flush()
    session.execute(
        update(ContentSearchRequest)
        .where(
            *scoped(ContentSearchRequest, project),
            ContentSearchRequest.asset_kind == kind,
        )
        .values(error=None)
    )
    return state


def index_status(session, project, *, access_context=None):
    result = {}
    for kind in KINDS:
        state = current_state(session, project, kind)
        conditions = (*scoped(ContentSearchRequest, project), ContentSearchRequest.asset_kind == kind)
        if access_context is not None:
            from ontology.content_access.service import document_condition, knowledge_condition

            visible = (
                document_condition(access_context, ContentSearchRequest.asset_id)
                if kind == "document"
                else ContentSearchRequest.asset_id.in_(
                    select(KnowledgePage.id).where(
                        *scoped(KnowledgePage, project),
                        knowledge_condition(access_context, revision=KnowledgePage.published_revision),
                    )
                )
            )
            conditions += (visible,)
        pending = session.scalar(
            select(func.count())
            .select_from(ContentSearchRequest)
            .where(
                *conditions,
                ContentSearchRequest.error.is_(None),
            )
        )
        errors = session.scalar(
            select(func.count())
            .select_from(ContentSearchRequest)
            .where(
                *conditions,
                ContentSearchRequest.error.is_not(None),
            )
        )
        status = (
            "not_ready"
            if not state
            else (
                "failed"
                if state.error or errors
                else "updating"
                if pending or not state.backfill_complete
                else "ready"
            )
        )
        result[kind] = {
            "status": status,
            "generation": state.generation if state else None,
            "pending": pending,
            "errors": errors,
            "tokenizer_version": TOKENIZER_VERSION,
            "chunker_version": CHUNKER_VERSION,
        }
    return result


def published_revision(session, page):
    return (
        session.scalar(
            select(KnowledgeRevision).where(
                KnowledgeRevision.tenant_id == page.tenant_id,
                KnowledgeRevision.project_id == page.project_id,
                KnowledgeRevision.page_id == page.id,
                KnowledgeRevision.revision == page.published_revision,
            )
        )
        if page.published_revision
        else None
    )


def _receipt(session, project, kind, asset):
    return session.scalar(
        select(ContentSearchDocument).where(
            *scoped(ContentSearchDocument, project),
            ContentSearchDocument.asset_kind == kind,
            ContentSearchDocument.asset_id == asset.id,
        )
    )


def _index_asset(session, project, state, asset):
    kind = state.kind
    revision = published_revision(session, asset) if kind == "knowledge" else None
    receipt = _receipt(session, project, kind, asset)
    if kind == "knowledge" and revision is None and receipt is None:
        # Unpublished drafts never contribute a receipt, title, snippet or count.
        request = session.scalar(
            select(ContentSearchRequest).where(
                *scoped(ContentSearchRequest, project),
                ContentSearchRequest.asset_kind == kind,
                ContentSearchRequest.asset_id == asset.id,
            )
        )
        if request:
            session.delete(request)
        return
    removed = asset.state not in {"ready", "partial"} if kind == "document" else revision is None
    if receipt is None:
        receipt = ContentSearchDocument(
            tenant_id=project.tenant_id,
            project_id=project.id,
            asset_kind=kind,
            asset_id=asset.id,
            document_id=asset.id if kind == "document" else None,
            page_id=asset.id if kind == "knowledge" else None,
        )
        session.add(receipt)
    receipt.source_revision = (
        asset.revision if kind == "document" else (revision.revision if revision else receipt.source_revision)
    )
    receipt.parse_revision = asset.parse_revision if kind == "document" else 0
    receipt.knowledge_revision_id = revision.id if revision else receipt.knowledge_revision_id
    receipt.generation, receipt.tokenizer_version, receipt.chunker_version = (
        state.generation,
        TOKENIZER_VERSION,
        CHUNKER_VERSION,
    )
    receipt.source_fingerprint = "0" * 64
    receipt.title = asset.filename if kind == "document" else revision.title if revision else receipt.title
    receipt.coverage = {}
    receipt.status, receipt.error, receipt.indexed_at = "removed", None, now()
    session.flush()
    session.execute(
        delete(ContentSearchChunk).where(
            *scoped(ContentSearchChunk, project),
            ContentSearchChunk.receipt_id == receipt.id,
        )
    )
    error = None
    if not removed:
        try:
            if kind == "document":
                ir = validate_document_ir(asset)
                fingerprint = ir_fingerprint(asset.ir)
                if asset.ir_fingerprint is not None and asset.ir_fingerprint != fingerprint:
                    raise DocumentError("invalid_document_ir", "解析指纹不一致，请重新解析文档")
                # Legacy rows are validated before first backfill, without
                # changing source revision, original bytes or parsed content.
                asset.ir_fingerprint = fingerprint
                receipt.source_fingerprint = fingerprint
                receipt.coverage = {
                    "complete": ir.complete,
                    "quality": ir.quality,
                    "warnings": ir.warnings,
                    "pages": [page.model_dump(mode="json") for page in ir.pages],
                }
                chunks = document_chunks(asset)
                receipt.status = "ready" if ir.complete else "partial"
            else:
                receipt.source_fingerprint = ir_fingerprint(
                    {
                        "id": revision.id,
                        "revision": revision.revision,
                        "title": revision.title,
                        "body": revision.body,
                        "references": revision.references,
                    }
                )
                receipt.coverage = {"complete": True, "quality": "published", "warnings": [], "pages": []}
                chunks = knowledge_chunks(revision)
                receipt.status = "ready"
            session.flush()
            batch = []
            for ordinal, chunk in enumerate(chunks):
                words = tokens(chunk["text"])
                batch.append(
                    {
                        "id": uid(),
                        "tenant_id": project.tenant_id,
                        "project_id": project.id,
                        "receipt_id": receipt.id,
                        "ordinal": ordinal,
                        "text": chunk["text"],
                        "token_text": " " + " ".join(words) + " ",
                        "anchor": chunk["anchor"],
                        "created_at": now(),
                    }
                )
                if len(batch) >= 500:
                    session.execute(insert(ContentSearchChunk), batch)
                    batch = []
            if batch:
                session.execute(insert(ContentSearchChunk), batch)
        except (DocumentError, SearchBudgetError) as exc:
            # No partial set of chunks is ever published as a successful index.
            error = str(exc)
            session.execute(
                delete(ContentSearchChunk).where(
                    *scoped(ContentSearchChunk, project),
                    ContentSearchChunk.receipt_id == receipt.id,
                )
            )
            receipt.status, receipt.error = "error", error
    request = session.scalar(
        select(ContentSearchRequest).where(
            *scoped(ContentSearchRequest, project),
            ContentSearchRequest.asset_kind == kind,
            ContentSearchRequest.asset_id == asset.id,
        )
    )
    if error:
        if request is None:
            enqueue_content(session, asset, kind)
            session.flush()
            request = session.scalar(
                select(ContentSearchRequest).where(
                    *scoped(ContentSearchRequest, project),
                    ContentSearchRequest.asset_kind == kind,
                    ContentSearchRequest.asset_id == asset.id,
                )
            )
        request.error, request.updated_at = error, now()
    elif request:
        session.delete(request)


def _pending(session, project, kind):
    return (
        session.scalar(
            select(ContentSearchRequest.id)
            .where(
                *scoped(ContentSearchRequest, project),
                ContentSearchRequest.asset_kind == kind,
                ContentSearchRequest.error.is_(None),
            )
            .limit(1)
        )
        is not None
    )


def _maintain(session, project, state):
    model = Document if state.kind == "document" else KnowledgePage
    if not state.backfill_complete:
        query = select(model).where(*scoped(model, project), model.id > state.backfill_cursor)
        if state.kind == "knowledge":
            query = query.where(KnowledgePage.published_revision.is_not(None))
        asset = session.scalar(query.order_by(model.id).limit(1))
        if asset is None:
            state.backfill_complete = True
        else:
            state.backfill_cursor = asset.id
    else:
        asset = session.scalar(
            select(model)
            .join(
                ContentSearchRequest,
                ContentSearchRequest.asset_id == model.id,
            )
            .where(
                *scoped(model, project),
                *scoped(ContentSearchRequest, project),
                ContentSearchRequest.asset_kind == state.kind,
                ContentSearchRequest.error.is_(None),
            )
            .order_by(ContentSearchRequest.updated_at, model.id)
            .limit(1)
        )
    if asset:
        _index_asset(session, project, state, asset)
    state.updated_at = now()


def process_one_content_index(db, tenant_id):
    project_id, kind, initial_generation, initial_updated_at = None, None, None, None
    try:
        with db.transaction(tenant_id) as session:
            candidates = []
            for candidate_kind in KINDS:
                state = exists(
                    select(ContentSearchState.id).where(
                        ContentSearchState.project_id == Project.id,
                        ContentSearchState.tenant_id == tenant_id,
                        ContentSearchState.kind == candidate_kind,
                    )
                )
                work = exists(
                    select(ContentSearchState.id).where(
                        ContentSearchState.project_id == Project.id,
                        ContentSearchState.tenant_id == tenant_id,
                        ContentSearchState.kind == candidate_kind,
                        or_(
                            ContentSearchState.tokenizer_version != TOKENIZER_VERSION,
                            ContentSearchState.chunker_version != CHUNKER_VERSION,
                            and_(
                                ContentSearchState.error.is_(None),
                                or_(
                                    ContentSearchState.backfill_complete.is_(False),
                                    exists(
                                        select(ContentSearchRequest.id).where(
                                            ContentSearchRequest.project_id == Project.id,
                                            ContentSearchRequest.tenant_id == tenant_id,
                                            ContentSearchRequest.asset_kind == candidate_kind,
                                            ContentSearchRequest.error.is_(None),
                                        )
                                    ).correlate(Project),
                                ),
                            ),
                        ),
                    )
                )
                candidates.append(or_(~state, work))
            project = session.scalar(
                select(Project)
                .where(
                    Project.tenant_id == tenant_id,
                    or_(*candidates),
                )
                .order_by(Project.id)
                .with_for_update(of=Project, skip_locked=True)
                .limit(1)
            )
            if project is None:
                return False
            project_id = project.id
            states = {
                row.kind: row
                for row in session.scalars(
                    select(ContentSearchState).where(
                        *scoped(ContentSearchState, project),
                    )
                )
            }
            choices = [
                k
                for k in KINDS
                if not states.get(k)
                or states[k].tokenizer_version != TOKENIZER_VERSION
                or states[k].chunker_version != CHUNKER_VERSION
                or (
                    not states[k].error and (not states[k].backfill_complete or _pending(session, project, k))
                )
            ]
            if not choices:
                return False
            kind = min(choices, key=lambda k: states[k].updated_at if k in states else "")
            old = states.get(kind)
            initial_generation, initial_updated_at = (old.generation, old.updated_at) if old else (None, None)
            state = current_state(session, project, kind) or reset_kind(session, project, kind)
            _maintain(session, project, state)
            return True
    except Exception as exc:  # noqa: BLE001 -- durable sanitized index boundary
        logger.error("Content index failed (%s), project=%s kind=%s", type(exc).__name__, project_id, kind)
        if project_id and kind:
            with db.transaction(tenant_id) as session:
                project = session.scalar(
                    select(Project)
                    .where(
                        Project.id == project_id,
                        Project.tenant_id == tenant_id,
                    )
                    .with_for_update()
                )
                state = session.scalar(
                    select(ContentSearchState).where(
                        *scoped(ContentSearchState, project),
                        ContentSearchState.kind == kind,
                    )
                )
                # A later rebuild or enqueue supersedes this failed attempt.
                if (state.generation if state else None, state.updated_at if state else None) == (
                    initial_generation,
                    initial_updated_at,
                ):
                    state = state or reset_kind(session, project, kind)
                    state.error, state.updated_at = "内容索引处理失败，请检查运行配置后重建", now()
        return False
