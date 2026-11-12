"""Exercise migration of a real pre-existing inbox, including PostgreSQL triggers."""

import hashlib
import json
import os
import subprocess
import sys
from unittest.mock import patch
from uuid import uuid4

import pytest
from ontology.cdc.models import CdcEvent, CdcStream
from ontology.cdc.runtime import stable_record
from ontology.core.config import Settings
from ontology.core.db import Database
from ontology.core.models import now
from ontology.sources.ingestion import fingerprint
from ontology.sources.models import Source
from sqlalchemy import MetaData, Table, create_engine, inspect, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError

from tests.support.cdc import native_event
from tests.support.cdc_evidence_migrations import historical_stream
from tests.support.identity import bootstrap_historical as bootstrap


@pytest.mark.parametrize(
    "dialect,has_duplicate", [("sqlite", False), ("postgresql", False), ("postgresql", True)]
)
def test_existing_inbox_identity_backfill_preserves_raw_event_and_immutability(
    tmp_path, dialect, has_duplicate
):
    provisioner, database_name = None, None
    if dialect == "postgresql":
        if not os.environ.get("ONTOLOGY_TEST_ADMIN_URL"):
            pytest.skip("Explicit PostgreSQL admin URL required")
        admin_url = make_url(os.environ["ONTOLOGY_TEST_ADMIN_URL"])
        database_name = "ontology_test_" + uuid4().hex
        provisioner = create_engine(admin_url, isolation_level="AUTOCOMMIT")
        with provisioner.connect() as connection:
            connection.execute(text(f'CREATE DATABASE "{database_name}"'))
        url = admin_url.set(database=database_name).render_as_string(hide_password=False)
    else:
        url = f"sqlite:///{tmp_path / 'old-inbox.db'}"
    db = Database(Settings(database_url=url, data_dir=tmp_path / "data"))

    def migrate(revision, *, succeeds=True):
        result = subprocess.run(
            [sys.executable, "-m", "alembic", "upgrade", revision],
            env={**os.environ, "ONTOLOGY_DATABASE_URL": url},
            text=True,
            capture_output=True,
            timeout=60,
            check=False,
        )
        if succeeds:
            assert result.returncode == 0, result.stderr
        return result

    try:
        migrate("783bb791c5d2")
        # Keep the historical schema intact while bootstrapping credentials.
        with patch.object(db, "create_schema"):
            owner = bootstrap(
                db,
                tenant_slug="migration-test",
                tenant_name="Migration",
                email="owner@example.test",
                password="Migration-Test-Password",
            )
        with db.transaction(owner["tenant_id"]) as session:
            source = Source(
                tenant_id=owner["tenant_id"],
                project_id=owner["project_id"],
                name="Old inbox",
                kind="postgres",
                config={},
            )
            session.add(source)
            session.flush()
            stream = historical_stream(
                session, owner, source.id, owner["project_id"], received_sequence=2 if has_duplicate else 1
            )
            raw = native_event(stream.generation)
            event_id = str(uuid4())
            old_table = Table("cdc_events", MetaData(), autoload_with=session.connection())
            session.execute(
                old_table.insert().values(
                    id=event_id,
                    tenant_id=owner["tenant_id"],
                    project_id=owner["project_id"],
                    created_at=now(),
                    stream_id=stream.id,
                    sequence=1,
                    event_id=raw["event_id"],
                    record_hash="b" * 64,
                    record=raw,
                )
            )
            if has_duplicate:
                duplicate = {
                    **raw,
                    "identity_material": json.dumps(json.loads(raw["identity_material"]), indent=2),
                }
                duplicate["event_id"] = hashlib.sha256(duplicate["identity_material"].encode()).hexdigest()
                session.execute(
                    old_table.insert().values(
                        id=str(uuid4()),
                        tenant_id=owner["tenant_id"],
                        project_id=owner["project_id"],
                        created_at=now(),
                        stream_id=stream.id,
                        sequence=2,
                        event_id=duplicate["event_id"],
                        record_hash="c" * 64,
                        record=duplicate,
                    )
                )
        if has_duplicate:
            result = migrate("head", succeeds=False)
            assert result.returncode != 0 and "share a semantic identity" in result.stderr
            with db.transaction(owner["tenant_id"]) as session:
                assert "identity_hash" not in {
                    column["name"] for column in inspect(session.connection()).get_columns("cdc_events")
                }
                retained = session.execute(
                    select(old_table.c.record, old_table.c.record_hash).order_by(old_table.c.sequence)
                ).all()
                assert retained == [(raw, "b" * 64), (duplicate, "c" * 64)]
                assert session.scalar(select(CdcStream.received_sequence)) == 2
            with pytest.raises(DBAPIError), db.transaction(owner["tenant_id"]) as session:
                session.execute(text("UPDATE cdc_events SET record_hash='changed'"))
            return
        migrate("head")
        with db.transaction(owner["tenant_id"]) as session:
            row = session.get(CdcEvent, event_id)
            assert row.record == raw
            assert row.event_id == raw["event_id"] and row.sequence == 1
            assert row.identity_hash == fingerprint(json.loads(raw["identity_material"]))
            assert row.record_hash == fingerprint(stable_record(raw))
            assert session.scalar(select(CdcStream.received_sequence)) == 1
            if dialect == "postgresql":
                assert session.execute(
                    text(
                        "SELECT relrowsecurity, relforcerowsecurity FROM pg_class WHERE relname='cdc_events'"
                    )
                ).one() == (True, True)
        if dialect == "postgresql":
            with pytest.raises(DBAPIError), db.transaction(owner["tenant_id"]) as session:
                session.execute(text("UPDATE cdc_events SET record_hash='changed'"))
    finally:
        db.engine.dispose()
        if provisioner:
            with provisioner.connect() as connection:
                connection.execute(text(f'DROP DATABASE "{database_name}" WITH (FORCE)'))
            provisioner.dispose()
