"""Independent regressions for content-search scheduling and failure fences."""

import hashlib
from concurrent.futures import ThreadPoolExecutor
from threading import Event
from uuid import uuid4

import pytest
from ontology.documents import parse_document
from ontology.documents.storage_models import Document
from ontology.identity.models import Project
from ontology.search.content import index
from ontology.search.content.models import (
    ContentSearchChunk,
    ContentSearchDocument,
    ContentSearchRequest,
    ContentSearchState,
)
from ontology.search.content.units import ir_fingerprint
from ontology.search.tokens import SearchBudgetError
from sqlalchemy import func, select

from tests.support.postgres import pg_databases as pg_databases  # noqa: PLC0414 - pytest fixture registration
from tests.support.postgres import pg_platform as pg_platform  # noqa: PLC0414 - pytest fixture registration


@pytest.fixture(params=["platform", "pg_platform"])
def content_race_fixture(request):
    return request.getfixturevalue(request.param)


def seed_document(db, owner, text="合成正文证据", *, project_id=None):
    data = text.encode("utf-8")
    identifier = str(uuid4())
    ir = parse_document(data, "evidence.txt", source_id=identifier, source_revision="1").model_dump(
        mode="json"
    )
    with db.transaction(owner["tenant_id"]) as session:
        session.add(
            Document(
                id=identifier,
                tenant_id=owner["tenant_id"],
                project_id=project_id or owner["project_id"],
                filename="evidence.txt",
                storage_key="synthetic-unused",
                content_hash=hashlib.sha256(data).hexdigest(),
                revision=1,
                parse_revision=1,
                ir_fingerprint=ir_fingerprint(ir),
                state="ready",
                ir=ir,
                size=len(data),
                uploader_id=owner["user_id"],
            )
        )
    return identifier


def retry_after_rollback(db, owner, document_id):
    with db.transaction(owner["tenant_id"]) as session:
        session.scalar(select(Project).where(Project.id == owner["project_id"]).with_for_update())
        index.enqueue_content(session, session.get(Document, document_id), "document")


def state_for(session, project_id, kind="document"):
    return session.scalar(
        select(ContentSearchState).where(
            ContentSearchState.project_id == project_id,
            ContentSearchState.kind == kind,
        )
    )


def drain(db, tenant_id):
    for _ in range(40):
        if not index.process_one_content_index(db, tenant_id):
            return
    pytest.fail("Content worker did not drain bounded synthetic fixture")


def test_first_failed_attempt_cannot_suppress_new_retry_before_state_exists(
    content_race_fixture, monkeypatch
):
    _, app, owner, _ = content_race_fixture
    db = app.state.db
    document_id = seed_document(db, owner)
    original = index._maintain
    intervened = []

    def fail_first_attempt(session, project, state):
        original(session, project, state)
        session.flush()
        raise RuntimeError("Synthetic failure during first content index transaction")

    def newer_committed_retry(*_):
        # The logger executes after rollback but before the separate failure
        # transaction. This is an actual intervening committed request, not a
        # fabricated generation or mutation of an in-memory ORM instance.
        retry_after_rollback(db, owner, document_id)
        intervened.append(document_id)

    with monkeypatch.context() as patch:
        patch.setattr(index, "_maintain", fail_first_attempt)
        patch.setattr(index.logger, "error", newer_committed_retry)
        assert not index.process_one_content_index(db, owner["tenant_id"])
    assert intervened == [document_id]
    with db.transaction(owner["tenant_id"]) as session:
        state = state_for(session, owner["project_id"])
        assert state is None or state.error is None, (
            "An obsolete first failure suppressed a newer durable retry"
        )
        request = session.scalar(
            select(ContentSearchRequest).where(ContentSearchRequest.asset_id == document_id)
        )
        assert request is not None and request.error is None
    drain(db, owner["tenant_id"])
    with db.transaction(owner["tenant_id"]) as session:
        receipt = session.scalar(
            select(ContentSearchDocument).where(ContentSearchDocument.asset_id == document_id)
        )
        assert receipt.status == "ready"


@pytest.mark.parametrize("intervention", ["enqueue", "rebuild"])
def test_old_failure_cannot_overwrite_existing_state_retry_or_new_generation(
    content_race_fixture, monkeypatch, intervention
):
    _, app, owner, _ = content_race_fixture
    db = app.state.db
    document_id = seed_document(db, owner)
    drain(db, owner["tenant_id"])
    retry_after_rollback(db, owner, document_id)

    def fail(*_):
        raise RuntimeError("Synthetic old attempt")

    generations = []

    def intervening_commit(*_):
        with db.transaction(owner["tenant_id"]) as session:
            project = session.scalar(
                select(Project).where(Project.id == owner["project_id"]).with_for_update()
            )
            if intervention == "rebuild":
                state = index.reset_kind(session, project, "document")
            else:
                index.enqueue_content(session, session.get(Document, document_id), "document")
                state = state_for(session, project.id)
            generations.append(state.generation)

    with monkeypatch.context() as patch:
        patch.setattr(index, "_maintain", fail)
        patch.setattr(index.logger, "error", intervening_commit)
        assert not index.process_one_content_index(db, owner["tenant_id"])
    with db.transaction(owner["tenant_id"]) as session:
        state = state_for(session, owner["project_id"])
        assert state.error is None and state.generation == generations[0]
    drain(db, owner["tenant_id"])


def test_nested_request_exists_selects_only_project_with_pending_work(content_race_fixture, monkeypatch):
    _, app, owner, _ = content_race_fixture
    db = app.state.db
    # Keep an earlier project fully idle; a request on a later project must not
    # make the earlier project appear eligible through an uncorrelated EXISTS.
    sibling_id = "ffffffff-ffff-4fff-bfff-ffffffffffff"
    with db.transaction(owner["tenant_id"]) as session:
        session.add(Project(id=sibling_id, tenant_id=owner["tenant_id"], name="Pending sibling"))
        session.flush()
        for project_id in (owner["project_id"], sibling_id):
            project = session.get(Project, project_id)
            for kind in index.KINDS:
                state = index.reset_kind(session, project, kind)
                state.backfill_complete = True
    document_id = seed_document(db, owner, project_id=sibling_id)
    with db.transaction(owner["tenant_id"]) as session:
        index.enqueue_content(session, session.get(Document, document_id), "document")
    original = index._maintain
    selected = []

    def record(session, project, state):
        selected.append(project.id)
        return original(session, project, state)

    monkeypatch.setattr(index, "_maintain", record)
    assert index.process_one_content_index(db, owner["tenant_id"])
    assert selected == [sibling_id]
    assert not index.process_one_content_index(db, owner["tenant_id"])
    with db.transaction(owner["tenant_id"]) as session:
        assert session.scalar(select(func.count()).select_from(ContentSearchChunk)) > 0


def test_budget_failure_after_flushed_chunk_batch_removes_all_chunks_and_isolates_asset(
    content_race_fixture, monkeypatch
):
    _, app, owner, _ = content_race_fixture
    db = app.state.db
    failed_id = seed_document(db, owner, "\n".join("budgetfault source paragraph" for _ in range(510)))
    peer_id = seed_document(db, owner, "Independent valid evidence")
    original = index.tokens
    calls = []

    def fail_after_durable_batch(text):
        if "budgetfault" in text:
            calls.append(text)
            if len(calls) == 501:
                # The production code already executed its first 500-row
                # INSERT. The domain failure must delete the complete staged
                # set and retain the retry; it must not cancel the peer asset.
                raise SearchBudgetError("Synthetic token budget exceeded after chunk batch")
        return original(text)

    with monkeypatch.context() as patch:
        patch.setattr(index, "tokens", fail_after_durable_batch)
        drain(db, owner["tenant_id"])
    assert len(calls) == 501
    with db.transaction(owner["tenant_id"]) as session:
        failed = session.scalar(
            select(ContentSearchDocument).where(ContentSearchDocument.asset_id == failed_id)
        )
        peer = session.scalar(select(ContentSearchDocument).where(ContentSearchDocument.asset_id == peer_id))
        assert failed.status == "error" and peer.status == "ready"
        assert (
            session.scalar(select(ContentSearchChunk.id).where(ContentSearchChunk.receipt_id == failed.id))
            is None
        )
        assert (
            session.scalar(select(ContentSearchChunk.id).where(ContentSearchChunk.receipt_id == peer.id))
            is not None
        )
        request = session.scalar(
            select(ContentSearchRequest).where(ContentSearchRequest.asset_id == failed_id)
        )
        assert request.error == "Synthetic token budget exceeded after chunk batch"
    retry_after_rollback(db, owner, failed_id)
    drain(db, owner["tenant_id"])
    with db.transaction(owner["tenant_id"]) as session:
        recovered = session.scalar(
            select(ContentSearchDocument).where(ContentSearchDocument.asset_id == failed_id)
        )
        assert recovered.status == "ready"
        assert (
            session.scalar(
                select(func.count())
                .select_from(ContentSearchChunk)
                .where(ContentSearchChunk.receipt_id == recovered.id)
            )
            == 510
        )


def test_postgres_workers_skip_locked_project_without_duplicate_receipts(request, monkeypatch):
    _, app, owner, _ = request.getfixturevalue("pg_platform")
    db = app.state.db
    document_id = seed_document(db, owner)
    entered, release = Event(), Event()
    original = index._maintain

    def hold_project_lock(session, project, state):
        entered.set()
        assert release.wait(8), "Concurrent test did not release its own project lock"
        return original(session, project, state)

    with monkeypatch.context() as patch, ThreadPoolExecutor(max_workers=2) as pool:
        patch.setattr(index, "_maintain", hold_project_lock)
        first = pool.submit(index.process_one_content_index, db, owner["tenant_id"])
        try:
            assert entered.wait(5), "First worker never acquired the synthetic project's lock"
            second = pool.submit(index.process_one_content_index, db, owner["tenant_id"])
            assert second.result(timeout=3) is False
        finally:
            release.set()
        assert first.result(timeout=5) is True
    drain(db, owner["tenant_id"])
    with db.transaction(owner["tenant_id"]) as session:
        assert (
            session.scalar(
                select(func.count())
                .select_from(ContentSearchDocument)
                .where(ContentSearchDocument.asset_id == document_id)
            )
            == 1
        )
