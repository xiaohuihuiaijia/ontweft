"""Parse evidence schema on SQLite and isolated, restricted PostgreSQL."""

import hashlib
from uuid import uuid4

import pytest
from ontology.documents import parse_document
from ontology.documents.history_models import DocumentParseVersion
from ontology.documents.storage_models import Document
from ontology.identity.models import Project
from ontology.jobs.models import Job
from ontology.search.content.units import ir_fingerprint
from sqlalchemy import MetaData, Table, delete, select, text, update
from sqlalchemy.exc import DBAPIError

from tests.support.cdc_evidence_migrations import constraint_rejection, isolated_database, seed_old
from tests.support.content_search_migration import seed_legacy_content
from tests.support.postgres import pg_databases as pg_databases  # noqa: PLC0414 - pytest fixture registration
from tests.support.postgres import pg_platform as pg_platform  # noqa: PLC0414 - pytest fixture registration

HEAD = "f2c7d9a41b60"


def parents(db, owner, project_id=None):
    scope = {"tenant_id": owner["tenant_id"], "project_id": project_id or owner["project_id"]}
    identifier = str(uuid4())
    payload = "解析证据😀\n精确来源与版本".encode()
    ir = parse_document(payload, "evidence.txt", source_id=identifier, source_revision="1").model_dump(
        mode="json"
    )
    with db.transaction(owner["tenant_id"]) as session:
        document = Document(
            **scope,
            id=identifier,
            filename="evidence.txt",
            storage_key="synthetic-only",
            content_hash=hashlib.sha256(payload).hexdigest(),
            size=len(payload),
            uploader_id=owner["user_id"],
            ir=ir,
            state="ready",
            parse_revision=1,
            ir_fingerprint=ir_fingerprint(ir),
        )
        job = Job(
            **scope,
            kind="document.parse",
            owner_id=owner["user_id"],
            status="succeeded",
            payload={"document_id": identifier, "revision": 1, "engine": "basic"},
        )
        session.add_all([document, job])
        session.flush()
        return {
            **scope,
            "document_id": identifier,
            "source_revision": 1,
            "parse_revision": 1,
            "content_hash": document.content_hash,
            "ir_fingerprint": document.ir_fingerprint,
            "ir": ir,
            "parser": ir["parser"],
            "parser_version": ir["parser_version"],
            "configuration_fingerprint": ir_fingerprint({}),
            "engine": "basic",
            "origin": "worker",
            "job_id": job.id,
        }


@pytest.fixture(params=["platform", "pg_platform"])
def parse_fixture(request):
    _, app, owner, other = request.getfixturevalue(request.param)
    return app.state.db, owner, other, parents(app.state.db, owner)


def test_parse_snapshot_round_trips_full_ir_configuration_and_legacy_zero(parse_fixture):
    db, owner, _, fields = parse_fixture
    with db.transaction(owner["tenant_id"]) as session:
        current = DocumentParseVersion(**fields)
        legacy = DocumentParseVersion(
            **{
                **fields,
                "parse_revision": 0,
                "origin": "legacy",
                "engine": "legacy",
                "job_id": None,
            }
        )
        later_source = DocumentParseVersion(
            **{
                **fields,
                "source_revision": 2,
                "parse_revision": 0,
                "origin": "legacy",
                "engine": "legacy",
                "job_id": None,
            }
        )
        session.add_all([current, legacy, later_source])
        session.flush()
        ids = [row.id for row in (current, legacy, later_source)]
        assert current.configuration == {}
    with db.transaction(owner["tenant_id"]) as session:
        rows = [session.get(DocumentParseVersion, identifier) for identifier in ids]
        assert [row.ir for row in rows] == [fields["ir"]] * 3
        assert rows[0].configuration_fingerprint == ir_fingerprint({})
        assert rows[1].job_id is None and rows[2].job_id is None
        assert rows[1].parse_revision == 0


def test_parse_checks_and_not_null_do_not_accept_invalid_history(parse_fixture):
    db, owner, _, fields = parse_fixture
    for changes in (
        {"source_revision": 0},
        {"parse_revision": -1},
        {"engine": "remote_guess"},
        {"origin": "inferred"},
        {"ir": None},
        {"configuration_fingerprint": None},
    ):
        with constraint_rejection(), db.transaction(owner["tenant_id"]) as session:
            session.add(DocumentParseVersion(**{**fields, **changes}))
            session.flush()
    with db.transaction(owner["tenant_id"]) as session:
        assert session.scalar(select(DocumentParseVersion.id)) is None


def test_parse_revision_and_job_unique_are_independent(parse_fixture):
    db, owner, _, fields = parse_fixture
    other_document = parents(db, owner)
    with db.transaction(owner["tenant_id"]) as session:
        session.add(DocumentParseVersion(**fields))
    for changes in (
        {"job_id": None},  # same source/parse revision, different provenance
        {"parse_revision": 2},  # same job cannot produce another version
    ):
        with constraint_rejection(), db.transaction(owner["tenant_id"]) as session:
            session.add(DocumentParseVersion(**{**fields, **changes}))
            session.flush()
    with constraint_rejection(), db.transaction(owner["tenant_id"]) as session:
        session.add(DocumentParseVersion(**{**other_document, "job_id": fields["job_id"]}))
        session.flush()
    with db.transaction(owner["tenant_id"]) as session:
        session.add(
            DocumentParseVersion(**{**fields, "parse_revision": 2, "job_id": other_document["job_id"]})
        )


def test_parse_scope_rejects_foreign_document_job_project_and_missing_job(parse_fixture):
    db, owner, other, fields = parse_fixture
    with db.transaction(owner["tenant_id"]) as session:
        project = Project(tenant_id=owner["tenant_id"], name="Separate parse project")
        session.add(project)
        session.flush()
        sibling_id = project.id
    sibling = parents(db, owner, sibling_id)
    foreign_tenant = parents(db, other)
    for changes in (
        {"document_id": sibling["document_id"]},
        {"job_id": sibling["job_id"]},
        {"project_id": sibling_id},
        {"job_id": str(uuid4())},
        {"document_id": foreign_tenant["document_id"]},
        {"job_id": foreign_tenant["job_id"]},
        {"project_id": other["project_id"], "job_id": None},
    ):
        with constraint_rejection(), db.transaction(owner["tenant_id"]) as session:
            session.add(DocumentParseVersion(**{**fields, **changes}))
            session.flush()
    with db.transaction(owner["tenant_id"]) as session:
        assert session.scalar(select(DocumentParseVersion.id)) is None


def test_postgres_parse_history_rls_runtime_privileges_and_owner_trigger(request):
    _, app, owner, other = request.getfixturevalue("pg_platform")
    admin, _ = request.getfixturevalue("pg_databases")
    db = app.state.db
    fields = parents(db, owner)
    with db.transaction(owner["tenant_id"]) as session:
        row = DocumentParseVersion(**fields)
        session.add(row)
        session.flush()
        identifier = row.id
        assert session.execute(
            text(
                "SELECT relrowsecurity, relforcerowsecurity FROM pg_class WHERE relname='document_parse_versions'"
            )
        ).one() == (True, True)
        assert session.execute(
            text(
                "SELECT has_table_privilege(current_user, 'document_parse_versions', 'SELECT'), "
                "has_table_privilege(current_user, 'document_parse_versions', 'INSERT'), "
                "has_table_privilege(current_user, 'document_parse_versions', 'UPDATE'), "
                "has_table_privilege(current_user, 'document_parse_versions', 'DELETE'), "
                "has_table_privilege(current_user, 'document_parse_versions', 'TRUNCATE')"
            )
        ).one() == (True, True, False, False, False)
    for tenant in (None, other["tenant_id"]):
        with db.transaction(tenant) as session:
            assert session.scalars(select(DocumentParseVersion)).all() == []
    with pytest.raises(DBAPIError) as rejected, db.transaction(other["tenant_id"]) as session:
        session.add(DocumentParseVersion(**{**fields, "parse_revision": 2, "job_id": None}))
        session.flush()
    assert rejected.value.orig.args[0]["C"] == "42501"
    # The runtime lacks mutation grants. The owner has them, so repeat with
    # that connection to exercise the immutable trigger rather than privileges.
    for connection_db in (db, admin):
        for statement in (
            update(DocumentParseVersion)
            .where(DocumentParseVersion.id == identifier)
            .values(parser="tampered"),
            delete(DocumentParseVersion).where(DocumentParseVersion.id == identifier),
        ):
            with (
                pytest.raises(DBAPIError) as rejected,
                connection_db.transaction(owner["tenant_id"]) as session,
            ):
                session.execute(statement)
            assert rejected.value.orig.args[0]["C"] == "42501"
            if connection_db is admin:
                assert "Immutable history" in rejected.value.orig.args[0]["M"]
    with db.transaction(owner["tenant_id"]) as session:
        row = session.get(DocumentParseVersion, identifier)
        assert row.ir == fields["ir"] and row.parser == fields["parser"]


@pytest.mark.parametrize("dialect", ["sqlite", "postgresql"])
def test_parse_migration_preserves_documents_jobs_citations_and_does_not_backfill(tmp_path, dialect):
    with isolated_database(tmp_path, dialect) as (db, migrate, _):
        legacy = seed_old(db)
        migrate("e8a42b6d901c")
        with db.transaction(legacy["owner"]["tenant_id"]) as session:
            document_id, _, _ = seed_legacy_content(session, legacy)
            session.add(
                Job(
                    **legacy["scope"],
                    kind="document.parse",
                    owner_id=legacy["owner"]["user_id"],
                    status="failed",
                    payload={"document_id": document_id, "revision": 3, "engine": "basic"},
                    attempts=2,
                    fencing_token=4,
                    error="Retained historical diagnostic",
                )
            )
            session.flush()
            names = ("documents", "jobs", "knowledge_pages", "knowledge_revisions", "source_record_revisions")
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
                after = [dict(row) for row in session.execute(select(table).order_by(table.c.id)).mappings()]
                assert after == before[name]
            assert session.scalar(select(DocumentParseVersion.id)) is None
            if dialect == "sqlite":
                assert session.execute(text("PRAGMA foreign_key_check")).all() == []
            else:
                assert session.execute(
                    text("SELECT relrowsecurity, relforcerowsecurity FROM pg_class WHERE relname='jobs'")
                ).one() == (True, True)
                assert (
                    session.scalar(
                        text(
                            "SELECT count(*) FROM pg_trigger t JOIN pg_class c ON c.oid=t.tgrelid "
                            "WHERE c.relname='document_parse_versions' AND t.tgname='immutable_history'"
                        )
                    )
                    == 1
                )
        # Check actual migrated-table constraints, independently of create_all.
        fields = parents(db, legacy["owner"])
        with db.transaction(legacy["owner"]["tenant_id"]) as session:
            session.add(DocumentParseVersion(**fields))
        with constraint_rejection(), db.transaction(legacy["owner"]["tenant_id"]) as session:
            session.add(DocumentParseVersion(**{**fields, "job_id": None}))
            session.flush()
