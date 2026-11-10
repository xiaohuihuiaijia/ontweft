"""Real snapshot worker integration with the source contribution ledger."""

import sqlite3
from uuid import uuid4

import pytest
from ontology.cdc.models import CdcStream
from ontology.cdc.projection_models import CdcProjectionBinding
from ontology.identity.models import Project
from ontology.objects.contributions.models import ContributionRevision, SourceContribution
from ontology.objects.models import Entity, EntityRevision
from ontology.sources.ingestion import fingerprint
from ontology.sources.models import SourceIdentity
from sqlalchemy import delete, select

from tests.support.ingestion import capture, make_source
from tests.support.platform import publish


def test_snapshot_partial_required_mapping_creates_incomplete_revision_one(platform):
    client, app, owner, _ = platform
    source, resource, _, definition = make_source(client, app, owner["project_id"])
    definition["mappings"][0]["fields"].pop("name")
    publish(client, owner["project_id"], definition)
    result = capture(client, app, source, resource)
    assert result["snapshot"]["status"] == "complete", result
    assert result["snapshot"]["projection"]["created"] == 2
    with app.state.db.transaction(owner["tenant_id"]) as session:
        objects = session.scalars(select(Entity)).all()
        assert len(objects) == 2
        assert all(
            obj.data_status == "incomplete" and obj.revision == 1 and not obj.deleted for obj in objects
        )
        assert all(set(obj.properties) == {"quantity"} for obj in objects)
        history = session.scalars(select(EntityRevision)).all()
        assert len(history) == 2 and all(
            row.revision == 1 and row.data_status == "incomplete" for row in history
        )
        assert len(session.scalars(select(SourceContribution)).all()) == 2


def test_snapshot_lazily_bootstraps_verified_old_evidence_before_advancing_pointer(platform):
    client, app, owner, _ = platform
    source, resource, path, _ = make_source(client, app, owner["project_id"])
    first = capture(client, app, source, resource)
    assert first["snapshot"]["status"] == "complete"
    with app.state.db.transaction(owner["tenant_id"]) as session:
        # Simulate a pre-ledger installation with unchanged immutable records.
        session.execute(delete(ContributionRevision))
        session.execute(delete(SourceContribution))
        old_ids = {identity.last_record_id for identity in session.scalars(select(SourceIdentity))}
    with sqlite3.connect(path) as connection:
        connection.execute("UPDATE inventory SET quantity='81' WHERE id=1")
    second = capture(client, app, source, resource)
    assert second["snapshot"]["status"] == "complete", second
    with app.state.db.transaction(owner["tenant_id"]) as session:
        current = session.scalars(select(SourceContribution)).all()
        history = session.scalars(select(ContributionRevision)).all()
        assert len(current) == 2 and all(row.revision == 2 for row in current)
        assert len(history) == 4
        assert {row.record_id for row in history if row.revision == 1} == old_ids
        assert all(row.record_id not in old_ids for row in current)


def test_snapshot_absent_row_does_not_infer_source_withdrawal(platform):
    client, app, owner, _ = platform
    source, resource, path, _ = make_source(client, app, owner["project_id"])
    first = capture(client, app, source, resource)
    assert first["snapshot"]["status"] == "complete"
    with app.state.db.transaction(owner["tenant_id"]) as session:
        old = {row.id: (row.revision, row.record_id) for row in session.scalars(select(SourceContribution))}
    with sqlite3.connect(path) as connection:
        connection.execute("DELETE FROM inventory WHERE id=2")
    second = capture(client, app, source, resource)
    assert second["snapshot"]["status"] == "complete" and second["snapshot"]["projection"]["deleted"] == 0
    with app.state.db.transaction(owner["tenant_id"]) as session:
        current = session.scalars(select(SourceContribution)).all()
        assert len(current) == 2 and all(row.active for row in current)
        assert sum((row.revision, row.record_id) == old[row.id] for row in current) == 1
        assert all(obj.data_status == "active" for obj in session.scalars(select(Entity)))


def test_snapshot_projection_failure_rolls_back_current_and_history_ledgers(platform, monkeypatch):
    from ontology.sources import ingestion

    client, app, owner, _ = platform
    source, resource, path, _ = make_source(client, app, owner["project_id"])
    first = capture(client, app, source, resource)
    assert first["snapshot"]["status"] == "complete"
    with sqlite3.connect(path) as connection:
        connection.execute("UPDATE inventory SET quantity='99'")
    original = ingestion.replace_contribution
    calls = []

    def fail_after_materialization(*args, **kwargs):
        result = original(*args, **kwargs)
        calls.append(result["entity"].id)
        if len(calls) == 2:
            raise RuntimeError("synthetic failure after two real materializations")
        return result

    monkeypatch.setattr(ingestion, "replace_contribution", fail_after_materialization)
    second = capture(client, app, source, resource)
    assert second["snapshot"]["status"] == "failed" and len(second["items"]) == 2
    assert len(calls) == 2
    with app.state.db.transaction(owner["tenant_id"]) as session:
        assert {obj.properties["quantity"] for obj in session.scalars(select(Entity))} == {80, 20}
        assert all(row.revision == 1 for row in session.scalars(select(SourceContribution)))
        assert len(session.scalars(select(ContributionRevision)).all()) == 2
        assert len(session.scalars(select(EntityRevision)).all()) == 2


@pytest.mark.parametrize("same_resource", [True, False])
def test_cdc_projection_binding_blocks_only_bound_resource_snapshot_overwrite(platform, same_resource):
    client, app, owner, _ = platform
    source, resource, path, _ = make_source(client, app, owner["project_id"])
    assert capture(client, app, source, resource)["snapshot"]["status"] == "complete"
    with app.state.db.transaction(owner["tenant_id"]) as session:
        stream = CdcStream(
            tenant_id=owner["tenant_id"],
            project_id=owner["project_id"],
            source_id=source["id"],
            source_revision=source["revision"],
            generation=str(uuid4()),
            connector="postgresql",
            owner_id=owner["user_id"],
            token_hash="a" * 64,
        )
        session.add(stream)
        session.flush()
        resources = [resource] if same_resource else [{"resource_id": "other.resource"}]
        session.add(
            CdcProjectionBinding(
                tenant_id=owner["tenant_id"],
                project_id=owner["project_id"],
                stream_id=stream.id,
                source_id=source["id"],
                source_revision=source["revision"],
                generation=stream.generation,
                release_id=session.get(Project, owner["project_id"]).active_release_id,
                resources=resources,
                binding_hash=fingerprint(resources),
                owner_id=owner["user_id"],
            )
        )
    with sqlite3.connect(path) as connection:
        connection.execute("UPDATE inventory SET quantity='99'")
    result = capture(client, app, source, resource)
    assert len(result["items"]) == 2
    assert result["snapshot"]["status"] == ("failed" if same_resource else "complete"), result
    if same_resource:
        assert "CDC" in result["snapshot"]["error"]
    with app.state.db.transaction(owner["tenant_id"]) as session:
        assert {obj.properties["quantity"] for obj in session.scalars(select(Entity))} == (
            {80, 20} if same_resource else {99}
        )
