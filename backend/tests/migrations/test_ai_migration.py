"""Upgrade retained evidence, then exercise the actual new AI tables and tenant policy."""

import os
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from ontology.core.config import Settings
from ontology.core.db import Database
from ontology.core.models import now
from ontology.intelligence.models import ModelConnection
from ontology.intelligence.proposal_models import AIPage, AIProposal, DocumentRelationContribution
from ontology.objects.models import Entity, Relation
from sqlalchemy import MetaData, Table, create_engine, inspect, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError

from tests.support.cdc_evidence_migrations import constraint_rejection, seed_old
from tests.support.content_search_migration import seed_legacy_content

MODELS = (ModelConnection, AIProposal, AIPage, DocumentRelationContribution)
OLD = "ef311029a700"
NEW = "ef311113a200"


@contextmanager
def migration_database(tmp_path, dialect):
    """Never load .env; PostgreSQL always gets a fresh, explicitly authorized test database."""
    provisioner, database_name, runtime_url = None, None, None
    if dialect == "postgresql":
        admin_value = os.environ.get("ONTOLOGY_TEST_ADMIN_URL")
        runtime_value = os.environ.get("ONTOLOGY_TEST_RUNTIME_URL")
        if not admin_value or not runtime_value:
            pytest.skip("Explicit PostgreSQL admin/runtime test URLs required")
        admin = make_url(admin_value)
        database_name = "ontology_test_" + uuid4().hex
        provisioner = create_engine(admin, isolation_level="AUTOCOMMIT")
        with provisioner.connect() as connection:
            connection.execute(text(f'CREATE DATABASE "{database_name}"'))
        url = admin.set(database=database_name).render_as_string(hide_password=False)
        runtime_url = make_url(runtime_value).set(database=database_name)
    else:
        url = f"sqlite:///{tmp_path / 'ai-migration.db'}"
    settings = Settings(_env_file=None, environment="development", database_url=url, data_dir=tmp_path / "admin")
    db = Database(settings)
    root = Path(__file__).resolve().parents[3]
    config = Config(str(root / "alembic.ini"))
    config.set_main_option("script_location", str(root / "backend" / "migrations"))

    def migrate(revision):
        with patch("ontology.core.config.Settings", return_value=settings):
            command.upgrade(config, revision)

    try:
        migrate("a928dc57be12")
        yield db, migrate, runtime_url
    finally:
        db.engine.dispose()
        if provisioner:
            with provisioner.connect() as connection:
                connection.execute(text(f'DROP DATABASE "{database_name}" WITH (FORCE)'))
            provisioner.dispose()


@pytest.mark.parametrize("dialect", ["sqlite", "postgresql"])
def test_ai_upgrade_preserves_history_and_enforces_cache_identity_and_rls(tmp_path, dialect):
    with migration_database(tmp_path, dialect) as (db, migrate, runtime_url):
        legacy = seed_old(db)
        migrate(OLD)
        scope, owner = legacy["scope"], legacy["owner"]
        retained = (
            "documents",
            "knowledge_pages",
            "knowledge_revisions",
            "entities",
            "releases",
            "source_record_revisions",
            "source_identities",
            "sources",
            "projects",
        )
        with db.transaction(owner["tenant_id"]) as session:
            document_id, _, _ = seed_legacy_content(session, legacy)
            assert not {model.__tablename__ for model in MODELS} & set(
                inspect(session.connection()).get_table_names()
            )
            before = {}
            for name in retained:
                table = Table(name, MetaData(), autoload_with=session.connection())
                before[name] = list(session.execute(select(table).order_by(table.c.id)).mappings())
        migrate(NEW)
        with db.transaction(owner["tenant_id"]) as session:
            for name in retained:
                table = Table(name, MetaData(), autoload_with=session.connection())
                assert list(session.execute(select(table).order_by(table.c.id)).mappings()) == before[name]
            assert all(session.scalar(select(model.id)) is None for model in MODELS)
            if dialect == "sqlite":
                assert session.execute(text("PRAGMA foreign_key_check")).all() == []
            else:
                for model in MODELS:
                    assert session.execute(
                        text("SELECT relrowsecurity, relforcerowsecurity FROM pg_class WHERE relname=:name"),
                        {"name": model.__tablename__},
                    ).one() == (True, True)
            connection = ModelConnection(
                tenant_id=owner["tenant_id"],
                name="Synthetic compatibility test",
                base_url="https://models.example.test/v1",
                secret="",
                models=[],
                roles={},
            )
            session.add(connection)
            session.flush()
            entity = session.get(Entity, legacy["identity"]["entity_id"])
            proposal = AIProposal(
                **scope,
                owner_id=owner["user_id"],
                connection_id=connection.id,
                connection_revision=1,
                baseline_release_id=entity.release_id,
                inputs={"documents": [{"id": document_id}]},
            )
            relation = Relation(
                **scope,
                source_id=entity.id,
                target_id=entity.id,
                type_key="synthetic",
                properties={},
            )
            session.add_all([proposal, relation])
            session.flush()
            page_fields = {
                **scope,
                "document_id": document_id,
                "source_revision": 3,
                "page_number": 1,
                "cache_key": "a" * 64,
                "model": "synthetic-model",
                "result": {"text": "Synthetic"},
            }
            relation_fields = {
                **scope,
                "source_id": legacy["source_id"],
                "fact_key": "b" * 64,
                "proposal_id": proposal.id,
                "relation_id": relation.id,
                "evidence": [],
            }
            session.add_all([AIPage(**page_fields), DocumentRelationContribution(**relation_fields)])
            session.flush()
        for model, fields in ((AIPage, page_fields), (DocumentRelationContribution, relation_fields)):
            with constraint_rejection(), db.transaction(owner["tenant_id"]) as session:
                session.add(model(**fields))
                session.flush()
        if dialect == "postgresql":
            role = db.engine.dialect.identifier_preparer.quote(runtime_url.username)
            with db.engine.begin() as connection:
                connection.execute(text(f"GRANT USAGE ON SCHEMA public TO {role}"))
                for model in MODELS:
                    connection.execute(text(f'GRANT SELECT, INSERT ON "{model.__tablename__}" TO {role}'))
            runtime = Database(
                Settings(
                    _env_file=None,
                    environment="development",
                    database_url=runtime_url.render_as_string(hide_password=False),
                    data_dir=tmp_path / "runtime",
                )
            )
            try:
                with runtime.transaction(owner["tenant_id"]) as session:
                    assert all(session.scalar(select(model.id)) for model in MODELS)
                for tenant in (None, legacy["other"]["tenant_id"]):
                    with runtime.transaction(tenant) as session:
                        assert all(session.scalar(select(model.id)) is None for model in MODELS)
                with (
                    pytest.raises(DBAPIError) as denied,
                    runtime.transaction(legacy["other"]["tenant_id"]) as session,
                ):
                    session.add(
                        ModelConnection(
                            tenant_id=owner["tenant_id"],
                            name="Foreign tenant write",
                            base_url="https://models.example.test/v1",
                            secret="",
                            models=[],
                            roles={},
                        )
                    )
                    session.flush()
                assert denied.value.orig.args[0]["C"] == "42501"
            finally:
                runtime.engine.dispose()


@pytest.mark.parametrize("dialect", ["sqlite", "postgresql"])
def test_existing_proposal_gets_empty_notes_without_changing_checkpoint_or_output(tmp_path, dialect):
    with migration_database(tmp_path, dialect) as (db, migrate, _runtime_url):
        legacy = seed_old(db)
        migrate("ef311113a100")
        owner = legacy["owner"]
        with db.transaction(owner["tenant_id"]) as session:
            connection = ModelConnection(tenant_id=owner["tenant_id"], name="Synthetic", base_url="https://example.test/v1",
                                         secret="", models=[], roles={})
            session.add(connection)
            session.flush()
            table = Table("ai_proposals", MetaData(), autoload_with=session.connection())
            old = {**legacy["scope"], "id": str(uuid4()), "created_at": now(), "revision": 7, "status": "ready",
                   "owner_id": owner["user_id"], "connection_id": connection.id, "connection_revision": 1,
                   "inputs": {"instructions": "Synthetic historical goal"}, "definition": {"types": [], "mappings": []},
                   "candidates": [{"id": "retained-candidate"}], "relations": [], "gaps": [], "imported": {}}
            session.execute(table.insert().values(**old))
        migrate(NEW)
        with db.transaction(owner["tenant_id"]) as session:
            proposal = session.get(AIProposal, old["id"])
            assert proposal.model_notes == []
            assert proposal.revision == 7 and proposal.candidates == old["candidates"]
            assert proposal.inputs == old["inputs"] and proposal.status == "ready"
