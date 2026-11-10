"""Reference-only evidence migration; no worker or business cursor advancement."""

import json
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from ontology.api import create_app
from ontology.cdc.models import CdcEvent, CdcStream
from ontology.core.config import Settings
from ontology.core.db import Database
from ontology.sources.models import SourceIdentity, SourceRecordRevision
from sqlalchemy import JSON, MetaData, Table, inspect, select, text, update
from sqlalchemy.exc import DBAPIError

from tests.support.cdc_evidence_migrations import (
    NEW,
    OLD,
    PASSWORD,
    constraint_rejection,
    isolated_database,
    reference,
    runtime_db,
    seed_event,
    seed_old,
)


@pytest.mark.parametrize("dialect", ["sqlite", "postgresql"])
def test_migrate_old_evidence_and_reference_only_database_contract(tmp_path, dialect):
    with isolated_database(tmp_path, dialect) as (admin, migrate, runtime_url):
        data = seed_old(admin)
        event_id, stream_id, native = seed_event(admin, data["owner"])
        foreign_project, _, _ = seed_event(admin, data["owner"], data["second_project"])
        foreign_tenant, _, _ = seed_event(admin, data["other"])
        migrate(NEW)
        db = runtime_db(admin, runtime_url, tmp_path)
        try:
            with db.transaction(data["owner"]["tenant_id"]) as session:
                old = session.get(SourceRecordRevision, data["raw"]["id"])
                assert {key: getattr(old, key) for key in data["raw"]} == data["raw"]
                assert old.record_codec == "snapshot-json-v1" and old.cdc_event_id is None
                identity = session.get(SourceIdentity, data["identity"]["id"])
                assert {key: getattr(identity, key) for key in data["identity"]} == data["identity"]
                assert identity.last_record_codec == "snapshot-json-v1"
                row = reference(data["scope"], event_id)
                session.add(row)
                session.flush()
                reference_id = row.id
                nulls = session.execute(
                    text("""SELECT snapshot_id IS NULL, attempt IS NULL, key_hash IS NULL,
                    source_key IS NULL, identity_key IS NULL, "values" IS NULL, content_hash IS NULL
                    FROM source_record_revisions WHERE id=:id"""),
                    {"id": row.id},
                ).one()
                assert all(nulls)
                assert session.get(CdcEvent, event_id).record == native
                stream = session.execute(
                    select(
                        CdcStream.received_sequence, CdcStream.projected_sequence, CdcStream.indexed_sequence
                    ).where(CdcStream.id == stream_id)
                ).one()
                assert (stream.received_sequence, stream.projected_sequence, stream.indexed_sequence) == (
                    1,
                    0,
                    0,
                )
            # Reusing an existing event must not silently create another row.
            with constraint_rejection(), db.transaction(data["owner"]["tenant_id"]) as session:
                session.add(reference(data["scope"], event_id))
                session.flush()
            # All malformed branches use fresh events so unique(event_id) cannot
            # accidentally be the reason these negative cases pass.
            invalid = [
                {"record_codec": "snapshot-json-v1"},
                {"record_codec": "unknown"},
                {"snapshot_id": data["snapshot_id"]},
                {"attempt": 1},
                {"key_hash": "c" * 64},
                {"source_key": {}},
                {"values": {}},
                {"identity_key": {}},
                {"content_hash": "d" * 64},
                {"source_key": JSON.NULL},
                {"identity_key": JSON.NULL},
                {"values": JSON.NULL},
                {"cdc_event_id": None},
            ]
            for overrides in invalid:
                next_event, _, _ = seed_event(admin, data["owner"])
                with constraint_rejection(), db.transaction(data["owner"]["tenant_id"]) as session:
                    session.add(reference(data["scope"], next_event, **overrides))
                    session.flush()
            for wrong_event in (foreign_project, foreign_tenant):
                with constraint_rejection(), db.transaction(data["owner"]["tenant_id"]) as session:
                    session.add(reference(data["scope"], wrong_event))
                    session.flush()
            for codec in ("snapshot-json-v1", "cdc-native-reference-v1"):
                with constraint_rejection(), db.transaction(data["owner"]["tenant_id"]) as session:
                    session.execute(
                        update(SourceIdentity)
                        .where(SourceIdentity.id == data["identity"]["id"])
                        .values(last_record_id=reference_id, last_record_codec=codec)
                    )
            with db.transaction(data["owner"]["tenant_id"]) as session:
                identity = session.get(SourceIdentity, data["identity"]["id"])
                assert identity.last_record_id == data["raw"]["id"]
                assert identity.key_hash == data["raw"]["key_hash"]
                # Defaults preserve the existing snapshot insertion contract.
                row = SourceRecordRevision(
                    **data["scope"],
                    snapshot_id=data["snapshot_id"],
                    attempt=4,
                    key_hash="d" * 64,
                    source_key={"id": 2},
                    identity_key=None,
                    values={"name": None},
                    content_hash="e" * 64,
                )
                session.add(row)
                session.flush()
                assert row.record_codec == "snapshot-json-v1"
            if dialect == "postgresql":
                with db.transaction(data["other"]["tenant_id"]) as session:
                    assert session.get(SourceRecordRevision, reference_id) is None
                    assert session.get(CdcEvent, event_id) is None
                for table in ("source_record_revisions", "cdc_events"):
                    with db.transaction(data["owner"]["tenant_id"]) as session:
                        flags = session.execute(
                            text(
                                "SELECT relrowsecurity, relforcerowsecurity FROM pg_class WHERE relname=:table"
                            ),
                            {"table": table},
                        ).one()
                        assert flags == (True, True)
                    for statement in (f"UPDATE {table} SET created_at='changed'", f"DELETE FROM {table}"):
                        with pytest.raises(DBAPIError), db.transaction(data["owner"]["tenant_id"]) as session:
                            session.execute(text(statement))
            else:
                with db.transaction() as session:
                    assert session.execute(text("PRAGMA foreign_key_check")).all() == []

            # Check historical invariants above at the original revision, then
            # run today's API against today's complete schema.
            migrate("head")
            if db is not admin:
                db.engine.dispose()
                db = runtime_db(admin, runtime_url, tmp_path)
            settings = Settings(
                database_url=db.engine.url.render_as_string(hide_password=False), data_dir=tmp_path / "api"
            )
            with patch.object(Database, "create_schema", return_value=None):
                app = create_app(settings)
            with TestClient(app) as client:
                assert (
                    client.post(
                        "/api/auth/login",
                        json={
                            "tenant_slug": "evidence-a",
                            "email": "owner@example.test",
                            "password": PASSWORD,
                        },
                    ).status_code
                    == 200
                )
                listing = client.get(f"/api/snapshots/{data['snapshot_id']}/records").json()
                assert listing["total"] == 1 and listing["items"][0]["id"] == data["raw"]["id"]
                assert json.loads(listing["items"][0]["values_json"]) == data["raw"]["values"]
                detail = client.get(f"/api/source-records/{reference_id}")
                assert detail.status_code == 200
                assert detail.json()["record_codec"] == "cdc-native-reference-v1"
                assert detail.json()["cdc_event_id"] == event_id and detail.json()["values"] is None
        finally:
            if db is not admin:
                db.engine.dispose()


@pytest.mark.parametrize("dialect", ["sqlite", "postgresql"])
@pytest.mark.parametrize("invalid", ["record_scope", "identity_scope", "attempt"])
def test_preflight_rejects_bad_history_before_ddl_without_rewriting(tmp_path, dialect, invalid):
    with isolated_database(tmp_path, dialect) as (db, migrate, _):
        data = seed_old(db, invalid)
        result = migrate(NEW, succeeds=False)
        assert "Existing source evidence has an invalid origin or scope" in result.stderr
        with db.transaction(data["owner"]["tenant_id"]) as session:
            assert session.scalar(text("SELECT version_num FROM alembic_version")) == OLD
            columns = {
                c["name"] for c in inspect(session.connection()).get_columns("source_record_revisions")
            }
            assert "record_codec" not in columns and "cdc_event_id" not in columns
            old_table = Table("source_record_revisions", MetaData(), autoload_with=session.connection())
            retained = (
                session.execute(select(old_table).where(old_table.c.id == data["raw"]["id"])).mappings().one()
            )
            assert dict(retained) == data["raw"]
        if dialect == "postgresql":
            with pytest.raises(DBAPIError), db.transaction(data["owner"]["tenant_id"]) as session:
                session.execute(text("UPDATE source_record_revisions SET content_hash='changed'"))
