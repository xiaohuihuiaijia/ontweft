"""Reference-only evidence migration; no worker or business cursor advancement."""

import json
import os
import subprocess
import sys
from contextlib import contextmanager
from unittest.mock import patch
from uuid import uuid4

import pytest
from ontology.cdc.models import CdcEvent, CdcStream
from ontology.core.config import Settings
from ontology.core.db import Database
from ontology.core.models import now
from ontology.modeling.models import ChangeSet, Release
from ontology.sources.ingestion import fingerprint
from ontology.sources.models import Source, SourceRecordRevision, SourceSnapshot
from sqlalchemy import MetaData, Table, create_engine, inspect, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError, IntegrityError

from tests.support.cdc import native_event
from tests.support.identity import bootstrap_historical as bootstrap

OLD = "a928dc57be12"

NEW = "d407be3a9128"

PASSWORD = "Synthetic-Migration-Test-Password"


@contextmanager
def constraint_rejection():
    # pg8000 classifies CHECK violations as ProgrammingError; verify SQLSTATE
    # instead of accidentally accepting an unrelated database failure.
    with pytest.raises(DBAPIError) as rejected:
        yield
    detail = rejected.value.orig.args[0]
    if isinstance(detail, dict):
        assert detail["C"] in {"23502", "23503", "23505", "23514"}
    else:
        assert isinstance(rejected.value, IntegrityError)
        assert "constraint" in str(detail).lower()


@contextmanager
def isolated_database(tmp_path, dialect):
    provisioner, database_name, runtime_url = None, None, None
    if dialect == "postgresql":
        if not os.environ.get("ONTOLOGY_TEST_ADMIN_URL") or not os.environ.get("ONTOLOGY_TEST_RUNTIME_URL"):
            pytest.skip("Explicit PostgreSQL admin and restricted runtime URLs required")
        admin = make_url(os.environ["ONTOLOGY_TEST_ADMIN_URL"])
        database_name = "ontology_test_" + uuid4().hex
        provisioner = create_engine(admin, isolation_level="AUTOCOMMIT")
        with provisioner.connect() as connection:
            connection.execute(text(f'CREATE DATABASE "{database_name}"'))
        url = admin.set(database=database_name).render_as_string(hide_password=False)
        runtime_url = make_url(os.environ["ONTOLOGY_TEST_RUNTIME_URL"]).set(database=database_name)
    else:
        url = f"sqlite:///{tmp_path / 'evidence.db'}"
    db = Database(Settings(database_url=url, data_dir=tmp_path / "admin"))

    def migrate(revision, succeeds=True):
        result = subprocess.run(
            [sys.executable, "-m", "alembic", "upgrade", revision],
            env={**os.environ, "ONTOLOGY_DATABASE_URL": url},
            capture_output=True,
            text=True,
            timeout=90,
            check=False,
        )
        # Do not expose database URLs or connection errors in test output.
        assert (result.returncode == 0) == succeeds, "Unexpected migration process result"
        return result

    try:
        migrate(OLD)
        yield db, migrate, runtime_url
    finally:
        db.engine.dispose()
        if provisioner:
            with provisioner.connect() as connection:
                connection.execute(text(f'DROP DATABASE "{database_name}" WITH (FORCE)'))
            provisioner.dispose()


def seed_old(db, invalid=None):
    # Alembic already created the historical schema; bootstrap must not create
    # future SQLite tables from today's metadata in a migration fixture.
    with patch.object(db, "create_schema", return_value=None):
        owner = bootstrap(
            db,
            tenant_slug="evidence-a",
            tenant_name="Synthetic A",
            email="owner@example.test",
            password=PASSWORD,
        )
        other = bootstrap(
            db,
            tenant_slug="evidence-b",
            tenant_name="Synthetic B",
            email="owner@example.test",
            password=PASSWORD,
        )
    scope = {"tenant_id": owner["tenant_id"], "project_id": owner["project_id"]}
    with db.transaction(owner["tenant_id"]) as session:
        second_id = str(uuid4())
        projects = Table("projects", MetaData(), autoload_with=session.connection())
        session.execute(
            projects.insert().values(
                id=second_id, tenant_id=owner["tenant_id"], name="Other synthetic project", version=0
            )
        )
        source = Source(**scope, name="Synthetic old source", kind="postgres", config={})
        change = ChangeSet(
            **scope,
            title="Synthetic release",
            definition={"types": [], "mappings": []},
            author_id=owner["user_id"],
        )
        session.add_all([source, change])
        session.flush()
        release = Release(
            **scope,
            version=1,
            definition={"types": [], "mappings": []},
            changeset_id=change.id,
            published_by=owner["user_id"],
        )
        session.add(release)
        session.flush()
        snapshot = SourceSnapshot(
            **scope,
            source_id=source.id,
            source_revision=1,
            resource_id="public.items",
            schema_fingerprint="a" * 64,
            release_id=release.id,
            status="complete",
            attempt=3,
            record_count=1,
            consistency="transaction",
        )
        session.add(snapshot)
        session.flush()
        # Seed the historical schema, independently of later Entity columns.
        entity_id = str(uuid4())
        entities = Table("entities", MetaData(), autoload_with=session.connection())
        session.execute(
            entities.insert().values(
                id=entity_id,
                **scope,
                created_at=now(),
                updated_at=now(),
                revision=1,
                type_key="Item",
                properties={"name": "原对象"},
                evidence=[],
                release_id=release.id,
                deleted=False,
            )
        )
        record_id, identity_id = str(uuid4()), str(uuid4())
        raw = {
            "id": record_id,
            **scope,
            "created_at": now(),
            "snapshot_id": snapshot.id,
            "attempt": 0 if invalid == "attempt" else 3,
            "key_hash": fingerprint({"id": 9223372036854775807}),
            "source_key": {"id": 9223372036854775807},
            "identity_key": None,
            "values": {"name": "原始值", "amount": "12345678901234567890.00001", "nullable": None},
            "content_hash": "b" * 64,
        }
        if invalid == "record_scope":
            raw["project_id"] = second_id
        records = Table("source_record_revisions", MetaData(), autoload_with=session.connection())
        session.execute(records.insert().values(**raw))
        identity = {
            "id": identity_id,
            **scope,
            "created_at": now(),
            "source_id": source.id,
            "resource_id": "public.items",
            "key_hash": raw["key_hash"],
            "key_codec": "legacy-json-v1",
            "type_key": "Item",
            "entity_id": entity_id,
            "last_record_id": record_id,
            "mapping_key": "items",
        }
        if invalid == "identity_scope":
            identity["project_id"] = second_id
        identities = Table("source_identities", MetaData(), autoload_with=session.connection())
        session.execute(identities.insert().values(**identity))
        result = {
            "owner": owner,
            "other": other,
            "scope": scope,
            "raw": raw,
            "identity": identity,
            "snapshot_id": snapshot.id,
            "second_project": second_id,
            "source_id": source.id,
        }
    return result


def seed_event(db, owner, project_id=None):
    scope = {"tenant_id": owner["tenant_id"], "project_id": project_id or owner["project_id"]}
    with db.transaction(owner["tenant_id"]) as session:
        source = Source(**scope, name="Synthetic native evidence", kind="postgres", config={})
        session.add(source)
        session.flush()
        stream = CdcStream(
            **scope,
            source_id=source.id,
            source_revision=1,
            generation=str(uuid4()),
            connector="postgresql",
            owner_id=owner["user_id"],
            token_hash="a" * 64,
            received_sequence=1,
        )
        session.add(stream)
        session.flush()
        raw = native_event(stream.generation)
        event = CdcEvent(
            **scope,
            stream_id=stream.id,
            sequence=1,
            event_id=raw["event_id"],
            identity_hash=fingerprint(json.loads(raw["identity_material"])),
            record_hash=fingerprint(raw),
            record=raw,
        )
        session.add(event)
        session.flush()
        return event.id, stream.id, raw


def reference(scope, event_id, **overrides):
    return SourceRecordRevision(
        **{
            **scope,
            "record_codec": "cdc-native-reference-v1",
            "cdc_event_id": event_id,
            "source_key": None,
            "identity_key": None,
            "values": None,
            **overrides,
        }
    )


def runtime_db(admin, url, tmp_path):
    if url is None:
        return admin
    role = admin.engine.dialect.identifier_preparer.quote(url.username)
    with admin.engine.begin() as connection:
        connection.execute(text("REVOKE CREATE ON SCHEMA public FROM PUBLIC"))
        connection.execute(text(f"GRANT USAGE ON SCHEMA public TO {role}"))
        for table in inspect(connection).get_table_names():
            # Test history triggers with an explicitly granted UPDATE/DELETE as
            # well; forced RLS still applies to this non-owner runtime login.
            grants = "SELECT" if table in {"tenants", "alembic_version"} else "SELECT, INSERT, UPDATE, DELETE"
            connection.execute(text(f'GRANT {grants} ON TABLE "{table}" TO {role}'))
    db = Database(
        Settings(database_url=url.render_as_string(hide_password=False), data_dir=tmp_path / "runtime")
    )
    # This fixture intentionally runs an older migration: verify its actual
    # tables, rather than requiring all future tables in today's metadata.
    with db.engine.connect() as connection:
        assert connection.execute(
            text("SELECT rolsuper, rolbypassrls FROM pg_roles WHERE rolname=current_user")
        ).one() == (False, False)
        assert not connection.scalar(text("SELECT has_schema_privilege(current_user, 'public', 'CREATE')"))
        flags = connection.execute(
            text("""SELECT c.relname, c.relrowsecurity, c.relforcerowsecurity,
            pg_has_role(current_user, c.relowner, 'MEMBER') AS owns_table
            FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
            WHERE n.nspname='public' AND c.relkind='r' AND c.relname != 'alembic_version'""")
        ).all()
        assert flags and all(
            not row.owns_table
            and (row.relname == "tenants" or (row.relrowsecurity and row.relforcerowsecurity))
            for row in flags
        )
    return db
