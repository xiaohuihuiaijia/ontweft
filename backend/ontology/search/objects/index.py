"""Durable coalescing index requests and bounded transactional maintenance.

No result is computed outside the project transaction. PostgreSQL project locks
therefore fence competing maintenance and object/model mutations without a new
lease service. Search never treats an outbox insertion as indexing success.
"""

import logging

from sqlalchemy import and_, delete, exists, or_, select, update

from ontology.cdc.models import CdcStream
from ontology.cdc.projection_models import CdcProjectionBinding, CdcProjectionWindow
from ontology.core.models import now, uid
from ontology.identity.models import Project
from ontology.modeling.models import Release
from ontology.objects.models import Entity
from ontology.search.objects.models import (
    ObjectSearchDocument,
    ObjectSearchField,
    ObjectSearchRequest,
    SearchCdcCheckpoint,
    SearchProjectState,
)
from ontology.search.tokens import TOKENIZER_VERSION, SearchBudgetError, tokens

logger = logging.getLogger(__name__)
BATCH_SIZE = 10
MAX_OBJECT_CHARACTERS = 500000
MAX_OBJECT_TOKENS = 100000


def scoped(model, project):
    return (model.tenant_id == project.tenant_id, model.project_id == project.id)


def enqueue_entity(session, obj):
    """Called in the same transaction as every object revision; coalesce safely."""
    request = session.scalar(
        select(ObjectSearchRequest).where(
            ObjectSearchRequest.tenant_id == obj.tenant_id,
            ObjectSearchRequest.project_id == obj.project_id,
            ObjectSearchRequest.entity_id == obj.id,
        )
    )
    if request is None:
        request = ObjectSearchRequest(
            tenant_id=obj.tenant_id,
            project_id=obj.project_id,
            entity_id=obj.id,
            requested_revision=obj.revision,
        )
        session.add(request)
    request.requested_revision, request.error, request.updated_at = obj.revision, None, now()
    state = session.scalar(
        select(SearchProjectState).where(
            SearchProjectState.tenant_id == obj.tenant_id,
            SearchProjectState.project_id == obj.project_id,
        )
    )
    if state:
        state.error = None
        state.updated_at = now()


def reset_project(session, project):
    state = session.scalar(select(SearchProjectState).where(*scoped(SearchProjectState, project)))
    if state is None:
        state = SearchProjectState(tenant_id=project.tenant_id, project_id=project.id)
        session.add(state)
    state.release_id, state.generation = project.active_release_id, uid()
    state.tokenizer_version = TOKENIZER_VERSION
    state.backfill_cursor, state.backfill_complete, state.error = "", False, None
    state.updated_at = now()
    # The new generation is not declared ready here. Previous fields remain
    # until rebuilt, but cannot satisfy a current-generation query.
    session.flush()
    session.execute(
        update(ObjectSearchRequest).where(*scoped(ObjectSearchRequest, project)).values(error=None)
    )
    return state


def current_state(session, project):
    state = session.scalar(select(SearchProjectState).where(*scoped(SearchProjectState, project)))
    if (
        state
        and state.release_id == project.active_release_id
        and state.tokenizer_version == TOKENIZER_VERSION
    ):
        return state
    return None


def index_status(session, project):
    state = current_state(session, project)
    status = "not_ready"
    if state:
        failed = session.scalar(
            select(ObjectSearchRequest.id)
            .where(
                *scoped(ObjectSearchRequest, project),
                ObjectSearchRequest.error.is_not(None),
            )
            .limit(1)
        )
        pending = session.scalar(
            select(ObjectSearchRequest.id)
            .where(
                *scoped(ObjectSearchRequest, project),
                ObjectSearchRequest.error.is_(None),
            )
            .limit(1)
        )
        status = (
            "failed"
            if state.error or failed
            else ("updating" if not state.backfill_complete or pending else "ready")
        )
    return {
        "kind": "object_fulltext",
        "generation": state.generation if state else None,
        "tokenizer_version": TOKENIZER_VERSION,
        "status": status,
    }


def cdc_index_status(session, project, stream):
    status = index_status(session, project)
    checkpoint = session.scalar(
        select(SearchCdcCheckpoint).where(
            *scoped(SearchCdcCheckpoint, project),
            SearchCdcCheckpoint.stream_id == stream.id,
        )
    )
    binding = session.scalar(
        select(CdcProjectionBinding).where(
            *scoped(CdcProjectionBinding, project),
            CdcProjectionBinding.stream_id == stream.id,
        )
    )
    sequence = checkpoint.sequence if checkpoint and checkpoint.generation == status["generation"] else 0
    if not binding or not status["generation"]:
        state, message = "not_ready", "等待对象全文索引初始化及有效的投影绑定"
    elif binding.release_id != project.active_release_id:
        state, message = "not_ready", "投影绑定与当前模型不同，不能声明本代全文索引已追平"
    elif status["status"] == "failed":
        state, message = "failed", "全文索引有失败项，已提交的对象投影不受影响"
    elif sequence < stream.projected_sequence or status["status"] != "ready":
        state, message = "pending", "对象全文索引仍在处理，序号只在索引事务提交后推进"
    else:
        state, message = "ready", "对象全文已处理到该投影水位；向量与文档索引另行管理"
    return {"status": state, "sequence": sequence, "generation": status["generation"], "message": message}


def _field_chunks(entity, definition):
    result, characters, count = [], 0, 0
    for prop in definition["properties"]:
        value = entity.properties.get(prop["key"])
        if prop["data_type"] not in {"string", "enum"} or not isinstance(value, str):
            continue
        characters += len(value)
        if characters > MAX_OBJECT_CHARACTERS:
            raise SearchBudgetError("单对象文本超过 500000 字符索引预算，未截断发布")
        words = tokens(value, maximum=MAX_OBJECT_TOKENS - count)
        count += len(words)
        # One distinct lexeme per field posting. Repeated words do not create
        # duplicate rows; exact terms remain <=512 UTF-8 bytes by tokens().
        for position, word in enumerate(dict.fromkeys(words)):
            result.append((prop["key"], position, " " + word + " "))
    return result


def _index_entity(session, project, state, entity, definitions):
    receipt = session.scalar(
        select(ObjectSearchDocument).where(
            *scoped(ObjectSearchDocument, project),
            ObjectSearchDocument.entity_id == entity.id,
        )
    )
    if receipt is None:
        receipt = ObjectSearchDocument(
            tenant_id=project.tenant_id,
            project_id=project.id,
            entity_id=entity.id,
        )
        session.add(receipt)
    error, chunks = None, []
    removed = entity.deleted or entity.data_status != "active"
    try:
        if not removed:
            definition = definitions.get(entity.type_key)
            if definition is None:
                raise SearchBudgetError("当前模型缺少对象类型，须先核对模型")
            chunks = _field_chunks(entity, definition)
    except SearchBudgetError as exc:
        error = str(exc)
    # Flush pending new receipt only once every required field is assigned.
    receipt.entity_revision, receipt.release_id = entity.revision, project.active_release_id
    receipt.generation, receipt.tokenizer_version = state.generation, TOKENIZER_VERSION
    receipt.status = "error" if error else "removed" if removed else "ready"
    receipt.error, receipt.indexed_at = error, now()
    session.execute(
        delete(ObjectSearchField).where(
            *scoped(ObjectSearchField, project),
            ObjectSearchField.entity_id == entity.id,
        )
    )
    for key, chunk, text in chunks if not error else []:
        session.add(
            ObjectSearchField(
                tenant_id=project.tenant_id,
                project_id=project.id,
                entity_id=entity.id,
                field_key=key,
                chunk=chunk,
                token_text=text,
                generation=state.generation,
            )
        )
    request = session.scalar(
        select(ObjectSearchRequest).where(
            *scoped(ObjectSearchRequest, project),
            ObjectSearchRequest.entity_id == entity.id,
        )
    )
    if error:
        if request is None:
            request = ObjectSearchRequest(
                tenant_id=project.tenant_id,
                project_id=project.id,
                entity_id=entity.id,
                requested_revision=entity.revision,
            )
            session.add(request)
        request.error, request.updated_at = error, now()
    elif request:
        session.delete(request)


def _advance_checkpoints(session, project, state):
    """Only consecutive committed windows whose current objects are indexed."""
    changed = False
    bindings = session.scalars(
        select(CdcProjectionBinding).where(
            *scoped(CdcProjectionBinding, project),
            CdcProjectionBinding.release_id == project.active_release_id,
        )
    ).all()
    for binding in bindings:
        stream = session.get(CdcStream, binding.stream_id)
        checkpoint = session.scalar(
            select(SearchCdcCheckpoint).where(
                *scoped(SearchCdcCheckpoint, project),
                SearchCdcCheckpoint.stream_id == stream.id,
            )
        )
        if checkpoint is None:
            checkpoint = SearchCdcCheckpoint(
                tenant_id=project.tenant_id,
                project_id=project.id,
                stream_id=stream.id,
                binding_id=binding.id,
                generation=state.generation,
                sequence=0,
            )
            session.add(checkpoint)
        elif checkpoint.generation != state.generation:
            checkpoint.generation, checkpoint.sequence, checkpoint.window_id = state.generation, 0, None
        windows = session.scalars(
            select(CdcProjectionWindow)
            .where(
                *scoped(CdcProjectionWindow, project),
                CdcProjectionWindow.binding_id == binding.id,
                CdcProjectionWindow.end_sequence > checkpoint.sequence,
                CdcProjectionWindow.end_sequence <= stream.projected_sequence,
            )
            .order_by(CdcProjectionWindow.end_sequence)
            .limit(100)
        ).all()
        for window in windows:
            if window.start_sequence != checkpoint.sequence + 1:
                break
            if window.entity_ids:
                matched = session.scalars(
                    select(Entity.id)
                    .join(
                        ObjectSearchDocument,
                        ObjectSearchDocument.entity_id == Entity.id,
                    )
                    .where(
                        *scoped(Entity, project),
                        *scoped(ObjectSearchDocument, project),
                        Entity.id.in_(window.entity_ids),
                        ObjectSearchDocument.generation == state.generation,
                        ObjectSearchDocument.entity_revision == Entity.revision,
                        ObjectSearchDocument.release_id == project.active_release_id,
                        ObjectSearchDocument.tokenizer_version == TOKENIZER_VERSION,
                        or_(
                            and_(
                                Entity.deleted.is_(False),
                                Entity.data_status == "active",
                                ObjectSearchDocument.status == "ready",
                            ),
                            and_(
                                or_(Entity.deleted.is_(True), Entity.data_status != "active"),
                                ObjectSearchDocument.status == "removed",
                            ),
                        ),
                    )
                ).all()
                if set(matched) != set(window.entity_ids):
                    break
            checkpoint.sequence, checkpoint.window_id, checkpoint.updated_at = (
                window.end_sequence,
                window.id,
                now(),
            )
            changed = True
    return changed


def _maintain_project(session, project, state, batch_size):
    release = session.get(Release, project.active_release_id)
    definitions = {item["key"]: item for item in release.definition["types"]}
    entities = []
    if not state.backfill_complete:
        entities = session.scalars(
            select(Entity)
            .where(
                *scoped(Entity, project),
                Entity.id > state.backfill_cursor,
            )
            .order_by(Entity.id)
            .limit(batch_size)
        ).all()
        if entities:
            state.backfill_cursor = entities[-1].id
        if len(entities) < batch_size:
            state.backfill_complete = True
    else:
        entities = session.scalars(
            select(Entity)
            .join(
                ObjectSearchRequest,
                ObjectSearchRequest.entity_id == Entity.id,
            )
            .where(
                *scoped(Entity, project),
                *scoped(ObjectSearchRequest, project),
                ObjectSearchRequest.error.is_(None),
            )
            .order_by(ObjectSearchRequest.updated_at, Entity.id)
            .limit(batch_size)
        ).all()
    for entity in entities:
        _index_entity(session, project, state, entity, definitions)
    session.flush()
    advanced = _advance_checkpoints(session, project, state)
    state.updated_at = now()
    return bool(entities) or advanced


def process_one_index(db, tenant_id, *, batch_size=BATCH_SIZE):
    # A project is the existing serialization boundary for object and model
    # mutations. No locks are carried across external IO; tokenization is local.
    if not 1 <= batch_size <= 25:
        raise ValueError("Index batches must be bounded to 1..25 objects")
    project_id, attempt_generation, attempt_release, initial_generation = None, None, None, None
    initial_updated_at = None
    try:
        with db.transaction(tenant_id) as session:
            pending = exists(
                select(ObjectSearchRequest.id).where(
                    ObjectSearchRequest.project_id == Project.id,
                    ObjectSearchRequest.tenant_id == tenant_id,
                    ObjectSearchRequest.error.is_(None),
                )
            )
            cdc_pending = exists(
                select(CdcStream.id)
                .outerjoin(
                    SearchCdcCheckpoint,
                    SearchCdcCheckpoint.stream_id == CdcStream.id,
                )
                .join(CdcProjectionBinding, CdcProjectionBinding.stream_id == CdcStream.id)
                .where(
                    CdcStream.project_id == Project.id,
                    CdcStream.tenant_id == tenant_id,
                    CdcProjectionBinding.release_id == Project.active_release_id,
                    CdcStream.projected_sequence > 0,
                    or_(
                        SearchCdcCheckpoint.id.is_(None),
                        SearchCdcCheckpoint.sequence < CdcStream.projected_sequence,
                        SearchCdcCheckpoint.generation != SearchProjectState.generation,
                    ),
                )
            )
            project = session.scalar(
                select(Project)
                .outerjoin(
                    SearchProjectState,
                    SearchProjectState.project_id == Project.id,
                )
                .where(
                    Project.tenant_id == tenant_id,
                    Project.active_release_id.is_not(None),
                    or_(
                        SearchProjectState.id.is_(None),
                        SearchProjectState.release_id != Project.active_release_id,
                        SearchProjectState.tokenizer_version != TOKENIZER_VERSION,
                        and_(
                            SearchProjectState.error.is_(None),
                            or_(SearchProjectState.backfill_complete.is_(False), pending, cdc_pending),
                        ),
                    ),
                )
                .order_by(SearchProjectState.updated_at, Project.id)
                .with_for_update(of=Project, skip_locked=True)
                .limit(1)
            )
            if project is None:
                return False
            project_id = project.id
            old = session.scalar(select(SearchProjectState).where(*scoped(SearchProjectState, project)))
            initial_generation = old.generation if old else None
            initial_updated_at = old.updated_at if old else None
            attempt_release = project.active_release_id
            state = current_state(session, project) or reset_project(session, project)
            attempt_generation = state.generation
            return _maintain_project(session, project, state, batch_size)
    except Exception as exc:  # noqa: BLE001 -- index boundary persists a sanitized, fenced failure.
        # Persist a safe diagnostic, never source text/SQL parameters. A failed
        # transaction cannot leave fields, receipts or a checkpoint committed.
        logger.error("Object index maintenance failed (%s), project=%s", type(exc).__name__, project_id)
        if project_id:
            with db.transaction(tenant_id) as session:
                project = session.scalar(
                    select(Project)
                    .where(
                        Project.id == project_id,
                        Project.tenant_id == tenant_id,
                    )
                    .with_for_update()
                )
                state = session.scalar(select(SearchProjectState).where(*scoped(SearchProjectState, project)))
                generation = state.generation if state else None
                # Initialization itself may have rolled back. Never overwrite
                # a different generation committed by a later worker/rebuild.
                if (
                    project.active_release_id == attempt_release
                    and generation in {initial_generation, attempt_generation}
                    and (state.updated_at if state else None) == initial_updated_at
                ):
                    state = current_state(session, project) or reset_project(session, project)
                    state.error, state.updated_at = "全文索引处理失败，请核对运行配置后重建", now()
        return False
