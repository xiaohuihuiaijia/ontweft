"""Actual content-search migrations, scoped references and restricted PostgreSQL."""

from uuid import uuid4

import pytest
from ontology.documents.storage_models import Document
from ontology.identity.models import Project
from ontology.knowledge.models import KnowledgeRevision
from ontology.search.content.models import (
    ContentSearchChunk,
    ContentSearchDocument,
    ContentSearchRequest,
    ContentSearchState,
)
from sqlalchemy import MetaData, Table, delete, inspect, select, text
from sqlalchemy.exc import DBAPIError

from tests.support.cdc_evidence_migrations import constraint_rejection, isolated_database, seed_old
from tests.support.content_search_migration import (
    HEAD,
    MODELS,
    insert_projection,
    parents,
    seed_legacy_content,
    values,
)
from tests.support.content_search_migration import content_fixture as content_fixture  # noqa: PLC0414 - pytest fixture registration
from tests.support.postgres import pg_databases as pg_databases  # noqa: PLC0414 - pytest fixture registration
from tests.support.postgres import pg_platform as pg_platform  # noqa: PLC0414 - pytest fixture registration


def test_content_defaults_and_derived_rows_are_mutable_without_claiming_backfill(content_fixture):
    db, owner, _, parent = content_fixture
    with db.transaction(owner["tenant_id"]) as session:
        receipt = insert_projection(session, values(parent))
        state = session.scalar(select(ContentSearchState))
        assert state.backfill_cursor == "" and state.backfill_complete is False and state.error is None
        assert receipt.parse_revision == 0 and receipt.coverage == {}
        source = session.get(Document, parent["document"])
        assert source.parse_revision == 0 and source.ir_fingerprint is None
    with db.transaction(owner["tenant_id"]) as session:
        session.get(ContentSearchDocument, parent["receipt"]).status = "partial"
        session.scalar(select(ContentSearchChunk)).text = "更新原文"
        session.scalar(select(ContentSearchRequest)).error = "retryable"
        session.scalar(select(ContentSearchState)).error = "synthetic batch failure"
    with db.transaction(owner["tenant_id"]) as session:
        assert session.get(ContentSearchDocument, parent["receipt"]).status == "partial"
        assert session.scalar(select(ContentSearchChunk.text)) == "更新原文"
        assert session.scalar(select(ContentSearchRequest.error)) == "retryable"
        assert session.scalar(select(ContentSearchState.error)) == "synthetic batch failure"
        for model in (ContentSearchChunk, ContentSearchDocument, ContentSearchRequest, ContentSearchState):
            session.execute(delete(model))
        assert session.get(Document, parent["document"]) is not None
        assert session.get(KnowledgeRevision, parent["revision"]) is not None


def test_content_exact_origin_check_rejects_null_mixed_and_wrong_ids(content_fixture):
    db, owner, _, parent = content_fixture
    document = values(parent)
    knowledge = values(parent, "knowledge")
    invalid = []
    for model in (ContentSearchDocument, ContentSearchRequest):
        invalid.extend(
            (model, document[model], change)
            for change in (
                {"asset_kind": "unknown"},
                {"document_id": None},
                {"page_id": parent["page"]},
                {"asset_id": str(uuid4())},
            )
        )
        invalid.extend(
            (model, knowledge[model], change)
            for change in (
                {"page_id": None},
                {"document_id": parent["document"]},
                {"asset_id": parent["document"]},
            )
        )
    invalid.extend(
        [
            (
                ContentSearchDocument,
                document[ContentSearchDocument],
                {"knowledge_revision_id": parent["revision"]},
            ),
            (ContentSearchDocument, knowledge[ContentSearchDocument], {"knowledge_revision_id": None}),
            # Same project but a different page's revision must still be rejected.
            (
                ContentSearchDocument,
                knowledge[ContentSearchDocument],
                {"knowledge_revision_id": parent["other_revision"]},
            ),
        ]
    )
    for model, fields, override in invalid:
        with constraint_rejection(), db.transaction(owner["tenant_id"]) as session:
            session.add(model(**{**fields, **override}))
            session.flush()
    with db.transaction(owner["tenant_id"]) as session:
        insert_projection(session, knowledge)


def test_content_bounds_uniqueness_and_coalescing_are_enforced(content_fixture):
    db, owner, _, parent = content_fixture
    valid = values(parent)
    with db.transaction(owner["tenant_id"]) as session:
        session.add(ContentSearchDocument(**valid[ContentSearchDocument]))
    for model, override in (
        (ContentSearchState, {"kind": "invalid"}),
        (ContentSearchDocument, {"source_revision": 0}),
        (ContentSearchDocument, {"parse_revision": -1}),
        (ContentSearchDocument, {"status": "unverified-success"}),
        (ContentSearchChunk, {"ordinal": -1}),
    ):
        with constraint_rejection(), db.transaction(owner["tenant_id"]) as session:
            if model == ContentSearchDocument:
                # Delete valid derived parent so the failure tests the stated
                # CHECK rather than the independently tested unique key.
                session.execute(delete(ContentSearchDocument))
            session.add(model(**{**valid[model], **override}))
            session.flush()
    with constraint_rejection(), db.transaction(owner["tenant_id"]) as session:
        session.get(Document, parent["document"]).parse_revision = -1
        session.flush()
    with db.transaction(owner["tenant_id"]) as session:
        session.add_all(model(**fields) for model, fields in valid.items() if model != ContentSearchDocument)
    for model, fields in valid.items():
        with constraint_rejection(), db.transaction(owner["tenant_id"]) as session:
            session.add(model(**{**fields, "id": str(uuid4())}))
            session.flush()
    # Document and knowledge queues are independently coalesced in one project.
    with db.transaction(owner["tenant_id"]) as session:
        session.add(ContentSearchRequest(**values(parent, "knowledge")[ContentSearchRequest]))
        session.add(ContentSearchState(**values(parent, "knowledge")[ContentSearchState]))


def test_content_scope_fks_reject_foreign_project_parent_and_receipt(content_fixture):
    db, owner, other, parent = content_fixture
    with db.transaction(owner["tenant_id"]) as session:
        sibling = Project(tenant_id=owner["tenant_id"], name="Sibling content project")
        session.add(sibling)
        session.flush()
        sibling_id = sibling.id
    foreign = parents(db, owner, project_id=sibling_id)
    for kind in ("document", "knowledge"):
        valid = values(parent, kind)
        for model in (ContentSearchDocument, ContentSearchRequest):
            with constraint_rejection(), db.transaction(owner["tenant_id"]) as session:
                session.add(model(**{**valid[model], "project_id": sibling_id}))
                session.flush()
    with constraint_rejection(), db.transaction(owner["tenant_id"]) as session:
        session.add(
            ContentSearchDocument(
                **{
                    **values(parent, "knowledge")[ContentSearchDocument],
                    "knowledge_revision_id": foreign["revision"],
                }
            )
        )
        session.flush()
    with db.transaction(owner["tenant_id"]) as session:
        session.add(ContentSearchDocument(**values(parent)[ContentSearchDocument]))
    with constraint_rejection(), db.transaction(owner["tenant_id"]) as session:
        session.add(ContentSearchChunk(**{**values(parent)[ContentSearchChunk], "project_id": sibling_id}))
        session.flush()
    # A state has no asset FK, so it needs its own tenant/project FK too.
    with constraint_rejection(), db.transaction(owner["tenant_id"]) as session:
        session.add(
            ContentSearchState(**{**values(parent)[ContentSearchState], "project_id": other["project_id"]})
        )
        session.flush()


def test_content_postgres_forced_rls_and_no_history_trigger_on_derived_rows(request):
    _, app, owner, other = request.getfixturevalue("pg_platform")
    db = app.state.db
    parent = parents(db, owner)
    with db.transaction(owner["tenant_id"]) as session:
        insert_projection(session, values(parent))
        for model in MODELS:
            assert session.execute(
                text("SELECT relrowsecurity, relforcerowsecurity FROM pg_class WHERE relname=:name"),
                {"name": model.__tablename__},
            ).one() == (True, True)
            assert (
                session.scalar(
                    text(
                        "SELECT count(*) FROM pg_trigger t JOIN pg_class c ON c.oid=t.tgrelid "
                        "WHERE c.relname=:name AND NOT t.tgisinternal"
                    ),
                    {"name": model.__tablename__},
                )
                == 0
            )
    for tenant in (None, other["tenant_id"]):
        with db.transaction(tenant) as session:
            for model in MODELS:
                assert session.scalars(select(model)).all() == []
                assert session.execute(delete(model)).rowcount == 0
    for model, fields in values(parent).items():
        with pytest.raises(DBAPIError) as rejected, db.transaction(other["tenant_id"]) as session:
            session.add(model(**{**fields, "id": str(uuid4())}))
            session.flush()
        assert rejected.value.orig.args[0]["C"] == "42501"


@pytest.mark.parametrize("dialect", ["sqlite", "postgresql"])
def test_content_migration_preserves_raw_ir_published_history_and_starts_empty(tmp_path, dialect):
    with isolated_database(tmp_path, dialect) as (db, migrate, _):
        legacy = seed_old(db)
        migrate("c6a18e04f927")
        names = ("documents", "knowledge_pages", "knowledge_revisions", "entities", "source_record_revisions")
        with db.transaction(legacy["owner"]["tenant_id"]) as session:
            doc_id, _, revision_id = seed_legacy_content(session, legacy)
            before = {}
            for name in names:
                table = Table(name, MetaData(), autoload_with=session.connection())
                before[name] = [
                    dict(row) for row in session.execute(select(table).order_by(table.c.id)).mappings()
                ]
        migrate(HEAD)
        with db.transaction(legacy["owner"]["tenant_id"]) as session:
            for name in names:
                table = Table(name, MetaData(), autoload_with=session.connection())
                result = [dict(row) for row in session.execute(select(table).order_by(table.c.id)).mappings()]
                if name == "documents":
                    for row in result:
                        assert row.pop("parse_revision") == 0
                        assert row.pop("ir_fingerprint") is None
                assert result == before[name]
            assert all(session.scalar(select(model.id)) is None for model in MODELS)
            assert session.get(Document, doc_id).ir["elements"][0]["text"] == "旧原文😀"
            if dialect == "sqlite":
                assert "ix_content_search_chunks_fts" not in {
                    row["name"] for row in inspect(session.connection()).get_indexes("content_search_chunks")
                }
                assert session.execute(text("PRAGMA foreign_key_check")).all() == []
            else:
                definition = session.scalar(
                    text("SELECT indexdef FROM pg_indexes WHERE indexname='ix_content_search_chunks_fts'")
                )
                assert (
                    "USING gin" in definition and "to_tsvector('simple'::regconfig, token_text)" in definition
                )
                # Parent table ALTER must retain both forced RLS and history trigger.
                for name in ("documents", "knowledge_pages", "knowledge_revisions"):
                    assert session.execute(
                        text("SELECT relrowsecurity, relforcerowsecurity FROM pg_class WHERE relname=:name"),
                        {"name": name},
                    ).one() == (True, True)
                assert (
                    session.scalar(
                        text(
                            "SELECT count(*) FROM pg_trigger t JOIN pg_class c ON c.oid=t.tgrelid "
                            "WHERE c.relname='knowledge_revisions' AND t.tgname='immutable_history'"
                        )
                    )
                    == 1
                )
                parent = {
                    "scope": legacy["scope"],
                    "document": doc_id,
                    "generation": str(uuid4()),
                    "receipt": str(uuid4()),
                }
                # Fill the irrelevant keys to reuse the explicit document branch.
                parent.update(page=None, revision=None)
                insert_projection(session, values(parent))
                assert (
                    session.scalar(
                        text(
                            "SELECT receipt_id FROM content_search_chunks "
                            "WHERE to_tsvector('simple'::regconfig, token_text) "
                            "@@ plainto_tsquery('simple'::regconfig, :query)"
                        ),
                        {"query": "中文"},
                    )
                    == parent["receipt"]
                )
        if dialect == "postgresql":
            with pytest.raises(DBAPIError), db.transaction(legacy["owner"]["tenant_id"]) as session:
                session.get(KnowledgeRevision, revision_id).body = "must not rewrite published history"
                session.flush()
