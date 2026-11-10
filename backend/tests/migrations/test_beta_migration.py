"""Upgrade a populated pre-Beta source without rewriting its evidence paths."""

from uuid import uuid4

import pytest
from ontology.identity.models import Project
from ontology.modeling.models import ChangeSet, Release
from ontology.objects.models import Entity
from ontology.search.objects.index import process_one_index
from ontology.search.objects.models import ObjectSearchDocument, ObjectSearchField, SearchProjectState
from ontology.search.tokens import TOKENIZER_VERSION
from ontology.sources.models import Source, SourceFileVersion
from sqlalchemy import func, select, text

from tests.support.cdc_evidence_migrations import isolated_database, seed_old


@pytest.mark.parametrize("dialect", ["sqlite", "postgresql"])
def test_beta_upgrade_registers_legacy_upload_without_changing_original_config(tmp_path, dialect):
    with isolated_database(tmp_path, dialect) as (db, migrate, _runtime):
        legacy = seed_old(db)
        migrate("b4a2d7e9c601")
        config = {
            "filename": "retained.csv",
            "content_sha256": "a" * 64,
            "tables_sha256": "b" * 64,
            "version": 1,
        }
        with db.transaction(legacy["owner"]["tenant_id"]) as session:
            source = Source(
                **legacy["scope"], name="Legacy immutable upload", kind="tabular", config=config, resources=[]
            )
            session.add(source)
            session.flush()
            source_id = source.id
        migrate("head")
        with db.transaction(legacy["owner"]["tenant_id"]) as session:
            source = session.get(Source, source_id)
            version = session.scalar(
                select(SourceFileVersion).where(SourceFileVersion.source_id == source_id)
            )
            assert source.config == config == version.config
            assert source.revision == version.applied_revision == version.base_revision == 1
            assert version.number == 1
        if dialect == "postgresql":
            with db.engine.connect() as connection:
                assert connection.scalar(
                    text("SELECT relforcerowsecurity FROM pg_class WHERE relname='sources'")
                )
                assert connection.scalar(
                    text("SELECT relforcerowsecurity FROM pg_class WHERE relname='source_file_versions'")
                )


@pytest.mark.parametrize("dialect", ["sqlite", "postgresql"])
def test_upgrade_rebuilds_only_derived_search_terms(tmp_path, dialect):
    with isolated_database(tmp_path, dialect) as (db, migrate, _runtime):
        legacy = seed_old(db)
        migrate("ef311029a300")
        tenant = legacy["owner"]["tenant_id"]
        definition = {
            "types": [
                {
                    "key": "Item",
                    "label": "Item",
                    "properties": [{"key": "name", "label": "Name", "data_type": "string"}],
                    "relations": [],
                }
            ]
        }
        with db.transaction(tenant) as session:
            change = ChangeSet(
                **legacy["scope"],
                title="Search fixture",
                definition=definition,
                author_id=legacy["owner"]["user_id"],
            )
            session.add(change)
            session.flush()
            release = Release(
                **legacy["scope"],
                version=2,
                definition=definition,
                changeset_id=change.id,
                published_by=legacy["owner"]["user_id"],
            )
            session.add(release)
            session.flush()
            project = session.get(Project, legacy["owner"]["project_id"])
            project.active_release_id, project.version = release.id, 2
            entity = session.scalar(select(Entity).where(Entity.project_id == project.id))
            entity_id, original = entity.id, entity.properties
            generation = str(uuid4())
            session.add_all(
                [
                    SearchProjectState(
                        **legacy["scope"],
                        release_id=release.id,
                        generation=generation,
                        tokenizer_version=TOKENIZER_VERSION,
                        backfill_cursor=entity.id,
                        backfill_complete=True,
                    ),
                    ObjectSearchDocument(
                        **legacy["scope"],
                        entity_id=entity.id,
                        entity_revision=entity.revision,
                        release_id=release.id,
                        generation=generation,
                        tokenizer_version=TOKENIZER_VERSION,
                        status="ready",
                    ),
                    ObjectSearchField(
                        **legacy["scope"],
                        entity_id=entity.id,
                        field_key="name",
                        chunk=0,
                        token_text=" old multiword chunk ",
                        generation=generation,
                    ),
                ]
            )
        migrate("head")
        with db.transaction(tenant) as session:
            assert session.get(Entity, entity_id).properties == original
            assert session.scalar(select(func.count()).select_from(ObjectSearchField)) == 0
            assert session.scalar(select(func.count()).select_from(ObjectSearchDocument)) == 1
            state = session.scalar(select(SearchProjectState))
            assert not state.backfill_complete and state.backfill_cursor == ""
        for _ in range(10):
            if not process_one_index(db, tenant):
                break
        with db.transaction(tenant) as session:
            assert session.scalar(select(SearchProjectState)).backfill_complete
            terms = session.scalars(select(ObjectSearchField.token_text)).all()
            assert terms and all(" " not in term.strip() for term in terms)
            assert session.get(Entity, entity_id).properties == original
        if dialect == "postgresql":
            with db.engine.connect() as connection:
                assert connection.scalar(
                    text(
                        "SELECT bool_and(relforcerowsecurity) FROM pg_class WHERE relname IN ('object_search_fields', 'search_project_states')"
                    )
                )
