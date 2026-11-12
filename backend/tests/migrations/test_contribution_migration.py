"""The contribution migration must not guess history from current objects."""

from uuid import uuid4

import pytest
from ontology.core.models import now
from ontology.objects.models import Entity, EntityRevision
from sqlalchemy import MetaData, Table, select

from tests.support.cdc_evidence_migrations import isolated_database, seed_old


@pytest.mark.parametrize("dialect", ["sqlite", "postgresql"])
def test_contribution_migration_preserves_originals_without_fabricated_ledger(tmp_path, dialect):
    with isolated_database(tmp_path, dialect) as (db, migrate, _):
        data = seed_old(db)
        migrate("d407be3a9128")
        with db.transaction(data["owner"]["tenant_id"]) as session:
            history = Table("entity_revisions", MetaData(), autoload_with=session.connection())
            session.execute(
                history.insert().values(
                    id=str(uuid4()),
                    **data["scope"],
                    created_at=now(),
                    entity_id=data["identity"]["entity_id"],
                    revision=1,
                    properties={"name": "原对象"},
                    evidence=[],
                    actor_id=data["owner"]["user_id"],
                    reason="旧历史",
                )
            )
            records = Table("source_record_revisions", MetaData(), autoload_with=session.connection())
            identities = Table("source_identities", MetaData(), autoload_with=session.connection())
            original_records = list(session.execute(select(records)).mappings())
            original_identities = list(session.execute(select(identities)).mappings())
            original_history = list(session.execute(select(history)).mappings())
        migrate("8c4f2d9a6b13")
        with db.transaction(data["owner"]["tenant_id"]) as session:
            assert list(session.execute(select(records)).mappings()) == original_records
            assert list(session.execute(select(identities)).mappings()) == original_identities
            assert list(session.execute(select(history)).mappings()) == original_history
            assert session.scalar(select(Entity.data_status)) == "active"
            for name in ("source_contributions", "contribution_revisions"):
                table = Table(name, MetaData(), autoload_with=session.connection())
                assert list(session.execute(select(table))) == []
            assert session.scalar(select(EntityRevision)).data_status == "active"
