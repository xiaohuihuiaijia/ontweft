"""Derived full-text schema: real migrations, scoped FKs and restricted PG RLS."""

from uuid import uuid4

import pytest
from ontology.cdc.models import CdcStream
from ontology.cdc.projection_models import CdcProjectionBinding, CdcProjectionWindow
from ontology.identity.models import Project
from ontology.modeling.models import ChangeSet, Release
from ontology.objects.models import Entity
from ontology.search.objects.models import (
    ObjectSearchDocument,
    ObjectSearchField,
    ObjectSearchRequest,
    SearchCdcCheckpoint,
    SearchProjectState,
)
from ontology.sources.models import Source
from sqlalchemy import MetaData, Table, delete, inspect, select, text
from sqlalchemy.exc import DBAPIError

from tests.support.cdc_evidence_migrations import (
    constraint_rejection,
    isolated_database,
    seed_event,
    seed_old,
)
from tests.support.postgres import pg_databases as pg_databases  # noqa: PLC0414 - pytest fixture registration
from tests.support.postgres import pg_platform as pg_platform  # noqa: PLC0414 - pytest fixture registration

MODELS = (
    ObjectSearchDocument,
    ObjectSearchField,
    ObjectSearchRequest,
    SearchProjectState,
    SearchCdcCheckpoint,
)


def parents(db, owner, *, project_id=None):
    scope = {"tenant_id": owner["tenant_id"], "project_id": project_id or owner["project_id"]}
    with db.transaction(owner["tenant_id"]) as session:
        change = ChangeSet(**scope, title="Search fixture", definition={}, author_id=owner["user_id"])
        source = Source(**scope, name="Search fixture", kind="postgres", config={})
        session.add_all([change, source])
        session.flush()
        release = Release(
            **scope, version=1, definition={}, changeset_id=change.id, published_by=owner["user_id"]
        )
        stream = CdcStream(
            **scope,
            source_id=source.id,
            source_revision=1,
            generation=str(uuid4()),
            connector="postgresql",
            owner_id=owner["user_id"],
            token_hash=uuid4().hex * 2,
        )
        session.add_all([release, stream])
        session.flush()
        entity = Entity(**scope, type_key="Item", properties={"name": "中文项目"}, release_id=release.id)
        binding = CdcProjectionBinding(
            **scope,
            stream_id=stream.id,
            source_id=source.id,
            source_revision=1,
            generation=stream.generation,
            release_id=release.id,
            resources=[],
            binding_hash="b" * 64,
            owner_id=owner["user_id"],
        )
        session.add_all([entity, binding])
        session.flush()
        window = CdcProjectionWindow(
            **scope,
            stream_id=stream.id,
            binding_id=binding.id,
            start_sequence=1,
            end_sequence=5,
            kind="snapshot",
            proof={"synthetic": True},
            schema_fingerprints={},
            result={},
            entity_ids=[entity.id],
            source_position={},
        )
        session.add(window)
        session.flush()
        return {
            "scope": scope,
            "entity": entity.id,
            "release": release.id,
            "stream": stream.id,
            "binding": binding.id,
            "window": window.id,
            "generation": str(uuid4()),
        }


def values(parent):
    scope = parent["scope"]
    gen = parent["generation"]
    return {
        ObjectSearchDocument: dict(
            **scope,
            entity_id=parent["entity"],
            entity_revision=1,
            release_id=parent["release"],
            generation=gen,
            tokenizer_version="synthetic-test-v1",
            status="ready",
        ),
        ObjectSearchField: dict(
            **scope,
            entity_id=parent["entity"],
            field_key="name",
            chunk=0,
            token_text="中文 项目",
            generation=gen,
        ),
        ObjectSearchRequest: dict(**scope, entity_id=parent["entity"], requested_revision=1),
        SearchProjectState: dict(
            **scope,
            release_id=parent["release"],
            generation=gen,
            tokenizer_version="synthetic-test-v1",
        ),
        SearchCdcCheckpoint: dict(
            **scope,
            stream_id=parent["stream"],
            binding_id=parent["binding"],
            generation=gen,
        ),
    }


@pytest.fixture(params=["platform", "pg_platform"])
def search_fixture(request):
    fixture = request.getfixturevalue(request.param)
    _, app, owner, other = fixture
    parent = parents(app.state.db, owner)
    return app.state.db, owner, other, parent


def test_search_rows_are_mutable_derived_data_and_defaults_do_not_claim_success(search_fixture):
    db, owner, _, parent = search_fixture
    with db.transaction(owner["tenant_id"]) as session:
        rows = {model: model(**fields) for model, fields in values(parent).items()}
        session.add_all(rows.values())
        session.flush()
        assert rows[SearchProjectState].backfill_complete is False
        assert rows[SearchProjectState].backfill_cursor == ""
        assert rows[SearchProjectState].error is None
        assert rows[SearchCdcCheckpoint].sequence == 0
        assert rows[SearchCdcCheckpoint].window_id is None
    with db.transaction(owner["tenant_id"]) as session:
        session.scalar(select(ObjectSearchDocument)).status = "removed"
        session.scalar(select(ObjectSearchField)).token_text = "更新 中文"
        session.scalar(select(ObjectSearchRequest)).requested_revision = 2
        session.scalar(select(SearchProjectState)).error = "retryable synthetic fault"
        checkpoint = session.scalar(select(SearchCdcCheckpoint))
        checkpoint.sequence, checkpoint.window_id = 5, parent["window"]
    with db.transaction(owner["tenant_id"]) as session:
        assert session.scalar(select(ObjectSearchDocument.status)) == "removed"
        assert session.scalar(select(ObjectSearchField.token_text)) == "更新 中文"
        assert session.scalar(select(ObjectSearchRequest.requested_revision)) == 2
        assert session.scalar(select(SearchProjectState.error)) == "retryable synthetic fault"
        assert session.scalar(select(SearchCdcCheckpoint.sequence)) == 5
        # Completing full text must not imply any other projection finished.
        assert session.get(CdcStream, parent["stream"]).indexed_sequence == 0
        for model in MODELS:
            session.execute(delete(model))
    with db.transaction(owner["tenant_id"]) as session:
        assert all(session.scalar(select(model.id)) is None for model in MODELS)
        assert session.get(Entity, parent["entity"]) is not None


def test_search_unique_coalescing_and_nonnegative_guards(search_fixture):
    db, owner, _, parent = search_fixture
    valid = values(parent)
    invalid = [
        (ObjectSearchDocument, {"entity_revision": 0}),
        (ObjectSearchDocument, {"status": "pretend_indexed"}),
        (ObjectSearchField, {"chunk": -1}),
        (ObjectSearchRequest, {"requested_revision": 0}),
        (SearchCdcCheckpoint, {"sequence": -1}),
    ]
    for model, override in invalid:
        with constraint_rejection(), db.transaction(owner["tenant_id"]) as session:
            session.add(model(**{**valid[model], **override}))
            session.flush()
    with db.transaction(owner["tenant_id"]) as session:
        session.add_all(model(**fields) for model, fields in valid.items())
    for model, fields in valid.items():
        with constraint_rejection(), db.transaction(owner["tenant_id"]) as session:
            session.add(model(**fields))
            session.flush()


def test_search_foreign_keys_reject_cross_project_and_cross_stream_references(search_fixture):
    db, owner, other, parent = search_fixture
    with db.transaction(owner["tenant_id"]) as session:
        sibling = Project(tenant_id=owner["tenant_id"], name="Other search fixture project")
        session.add(sibling)
        session.flush()
        sibling_id = sibling.id
    different = parents(db, owner, project_id=sibling_id)
    valid = values(parent)
    for model, fields in valid.items():
        with constraint_rejection(), db.transaction(owner["tenant_id"]) as session:
            session.add(model(**{**fields, "project_id": sibling_id}))
            session.flush()
    for model in (ObjectSearchDocument, SearchProjectState):
        with constraint_rejection(), db.transaction(owner["tenant_id"]) as session:
            session.add(model(**{**valid[model], "release_id": different["release"]}))
            session.flush()
    for overrides in (
        {"stream_id": different["stream"]},
        {"binding_id": different["binding"]},
        {"window_id": different["window"]},
        {"window_id": str(uuid4())},
    ):
        with constraint_rejection(), db.transaction(owner["tenant_id"]) as session:
            session.add(SearchCdcCheckpoint(**{**valid[SearchCdcCheckpoint], **overrides}))
            session.flush()
    # Even an empty project with no release is protected by the project-scope
    # FK; a simple project_id FK plus tenant RLS would not catch this mismatch.
    with constraint_rejection(), db.transaction(owner["tenant_id"]) as session:
        session.add(
            SearchProjectState(
                **{
                    **valid[SearchProjectState],
                    "project_id": other["project_id"],
                    "release_id": None,
                }
            )
        )
        session.flush()
    # Nullable release genuinely supports an unpublished project.
    with db.transaction(owner["tenant_id"]) as session:
        session.add(SearchProjectState(**{**valid[SearchProjectState], "release_id": None}))


def test_postgres_search_rows_force_rls_without_immutable_history_triggers(request):
    _, app, owner, other = request.getfixturevalue("pg_platform")
    db = app.state.db
    parent = parents(db, owner)
    with db.transaction(owner["tenant_id"]) as session:
        session.add_all(model(**fields) for model, fields in values(parent).items())
        for model in MODELS:
            flags = session.execute(
                text("SELECT relrowsecurity, relforcerowsecurity FROM pg_class WHERE relname=:name"),
                {"name": model.__tablename__},
            ).one()
            assert flags == (True, True)
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
            session.add(model(**fields))
            session.flush()
        assert rejected.value.orig.args[0]["C"] == "42501"


@pytest.mark.parametrize("dialect", ["sqlite", "postgresql"])
def test_search_migration_preserves_objects_evidence_and_watermarks_and_leaves_indexes_unbuilt(
    tmp_path, dialect
):
    with isolated_database(tmp_path, dialect) as (db, migrate, _):
        legacy = seed_old(db)
        migrate("b9e5217ac340")
        seed_event(db, legacy["owner"])
        names = (
            "entities",
            "source_record_revisions",
            "source_identities",
            "cdc_streams",
            "cdc_events",
            "releases",
            "projects",
        )
        with db.transaction(legacy["owner"]["tenant_id"]) as session:
            before = {}
            for name in names:
                table = Table(name, MetaData(), autoload_with=session.connection())
                before[name] = list(session.execute(select(table).order_by(table.c.id)).mappings())
        migrate("c6a18e04f927")
        with db.transaction(legacy["owner"]["tenant_id"]) as session:
            for name in names:
                table = Table(name, MetaData(), autoload_with=session.connection())
                assert list(session.execute(select(table).order_by(table.c.id)).mappings()) == before[name]
            assert all(session.scalar(select(model.id)) is None for model in MODELS)
            assert session.scalar(
                select(Entity.properties).where(Entity.id == legacy["identity"]["entity_id"])
            ) == {"name": "原对象"}
            if dialect == "sqlite":
                assert "ix_object_search_fields_fts" not in {
                    index["name"]
                    for index in inspect(session.connection()).get_indexes("object_search_fields")
                }
            else:
                definition = session.scalar(
                    text("SELECT indexdef FROM pg_indexes WHERE indexname='ix_object_search_fields_fts'")
                )
                assert (
                    "USING gin" in definition and "to_tsvector('simple'::regconfig, token_text)" in definition
                )
                # Exercise actual PostgreSQL full text on the migrated table.
                # This proves the expression contract, not a scale benchmark.
                session.add(
                    ObjectSearchField(
                        **legacy["scope"],
                        entity_id=legacy["identity"]["entity_id"],
                        field_key="name",
                        chunk=0,
                        token_text="中文 项目",
                        generation=str(uuid4()),
                    )
                )
                session.flush()
                assert (
                    session.scalar(
                        text(
                            "SELECT entity_id FROM object_search_fields "
                            "WHERE to_tsvector('simple'::regconfig, token_text) "
                            "@@ plainto_tsquery('simple'::regconfig, :query)"
                        ),
                        {"query": "中文"},
                    )
                    == legacy["identity"]["entity_id"]
                )
