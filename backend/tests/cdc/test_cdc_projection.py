"""Real API -> inbox -> leased job -> contributions, using native capture fixtures."""

import json
from copy import deepcopy

import pytest
from fastapi.testclient import TestClient
from ontology.cdc.models import CdcStream
from ontology.cdc.projection import schedule_pending
from ontology.cdc.projection_models import CdcProjectionState, CdcProjectionWindow
from ontology.connectors.base import ResourceSchema, canonical_identity_key
from ontology.core.domain import save_entity_revision
from ontology.identity.models import Membership, User
from ontology.jobs.models import Job
from ontology.objects.models import Entity
from ontology.sources.ingestion import fingerprint, schema_fingerprint
from ontology.sources.models import Source, SourceIdentity, SourceRecordRevision, SourceSnapshot
from ontology.worker import process_one
from sqlalchemy import select

from tests.support.cdc_projection import (
    ROOT,
    append_events,
    data_counts,
    fixtures,
    run,
    setup_projection,
    view,
)
from tests.support.platform import publish
from tests.support.postgres import pg_databases as pg_databases  # noqa: PLC0414 - pytest fixture registration
from tests.support.postgres import pg_platform as pg_platform  # noqa: PLC0414 - pytest fixture registration


@pytest.mark.parametrize("fixture_name", ["platform", "pg_platform"])
def test_projection_ignores_unrelated_source_drift(request, fixture_name, monkeypatch):
    state = setup_projection(request.getfixturevalue(fixture_name), monkeypatch)
    client, app, owner, _ = state["platform"]
    definition = client.get("/api/ontology/definition", params={"project_id": owner["project_id"]}).json()["definition"]
    with app.state.db.transaction(owner["tenant_id"]) as session:
        other = Source(tenant_id=owner["tenant_id"], project_id=owner["project_id"],
            name="Unrelated", kind="postgres", resources=deepcopy(state["resources"]))
        session.add(other)
        session.flush()
        other_id = other.id
    definition["mappings"].append({**deepcopy(definition["mappings"][0]),
        "key": "unrelated", "source_id": other_id})
    publish(client, owner["project_id"], definition)
    with app.state.db.transaction(owner["tenant_id"]) as session:
        session.get(Source, other_id).resources = []
    append_events(state, fixtures()[:6])
    result = run(state)
    assert result["status"] == "caught_up", result
    assert result["projected_sequence"] == 6 and data_counts(state)["entities"] == 2


@pytest.mark.parametrize("fixture_name", ["platform", "pg_platform"])
def test_native_snapshot_and_toast_transaction_materialize_ordinary_objects(
    request, fixture_name, monkeypatch
):
    state = setup_projection(request.getfixturevalue(fixture_name), monkeypatch)
    native = fixtures()
    append_events(state, native[:6])
    initial = run(state, automatic=True)
    assert initial["status"] == "caught_up", initial
    assert initial["projected_sequence"] == 6 and initial["indexed_sequence"] == 0
    assert initial["baseline"]["proof_kind"] == "completed_notification"
    assert data_counts(state)["entities"] == 2
    client, app, owner, _ = state["platform"]
    with app.state.db.transaction(owner["tenant_id"]) as session:
        entity = session.scalar(
            select(Entity).where(Entity.project_id == owner["project_id"]).order_by(Entity.created_at)
        )
        assert entity.revision == 1
        assert entity.properties["amount"] == "123456789012.123456789012345678"
        entity_id = entity.id
    for offset in (6, 9, 12):
        append_events(state, native[offset : offset + 3])
        committed = run(state, automatic=True)
        assert committed["status"] == "caught_up", committed
        obj = client.get(f"/api/objects/{entity_id}").json()
        assert obj["data_status"] == "active"
        if offset in (6, 9):
            assert len(obj["properties"]["optional"]) == 36_000
        else:
            assert obj["properties"]["optional"] is None
    before = data_counts(state)
    assert append_events(state, native)["inserted"] == 0
    assert not schedule_pending(app.state.db, owner["tenant_id"])
    assert data_counts(state) == before
    with app.state.db.transaction(owner["tenant_id"]) as session:
        records = session.scalars(
            select(SourceRecordRevision).where(SourceRecordRevision.project_id == owner["project_id"])
        ).all()
        assert all(
            r.record_codec == "cdc-native-reference-v1"
            and r.values is None
            and r.source_key is None
            and r.snapshot_id is None
            for r in records
        )
        windows = session.scalars(
            select(CdcProjectionWindow).where(CdcProjectionWindow.project_id == owner["project_id"])
        ).all()
        assert len(windows) == 4 and all(w.entity_ids for w in windows)


def test_partial_snapshot_waits_without_publishing_then_completion_wakes_job(platform, monkeypatch):
    state = setup_projection(platform, monkeypatch)
    native = fixtures()
    append_events(state, native[:5])
    pending = run(state, automatic=True)
    assert pending["status"] == "waiting" and pending["projected_sequence"] == 0
    assert pending["baseline"] is None and data_counts(state)["entities"] == 0
    assert not schedule_pending(platform[1].state.db, platform[2]["tenant_id"])
    append_events(state, native[5:6])
    assert run(state, automatic=True)["status"] == "caught_up"


@pytest.mark.parametrize("fixture_name", ["platform", "pg_platform"])
def test_failed_transaction_rolls_back_objects_contributions_history_and_cursor(
    request, fixture_name, monkeypatch
):
    state = setup_projection(request.getfixturevalue(fixture_name), monkeypatch)
    native = fixtures()
    append_events(state, native[:6])
    assert run(state)["status"] == "caught_up"
    before = data_counts(state)
    append_events(state, native[6:9])
    from ontology.cdc import projection

    original = projection.project_window

    def failure(*args, **kwargs):
        original(*args, **kwargs)
        raise RuntimeError("crash after writes before transaction commit")

    monkeypatch.setattr(projection, "project_window", failure)
    failed = run(state)
    assert failed["status"] == "failed" and failed["projected_sequence"] == 6
    assert data_counts(state) == before
    monkeypatch.setattr(projection, "project_window", original)
    retried = run(state)
    assert retried["status"] == "caught_up" and retried["projected_sequence"] == 9


def test_transaction_without_baseline_and_catalog_drift_block_without_mutation(platform, monkeypatch):
    state = setup_projection(platform, monkeypatch)
    append_events(state, fixtures()[6:9])
    result = run(state)
    assert result["status"] == "blocked" and result["reason_code"] == "baseline_required"
    assert result["projected_sequence"] == 0 and data_counts(state)["entities"] == 0
    assert not schedule_pending(platform[1].state.db, platform[2]["tenant_id"])


def test_catalog_drift_after_network_io_and_window_budget_fail_closed(platform, monkeypatch):
    state = setup_projection(platform, monkeypatch)
    append_events(state, fixtures()[:6])
    state["resources"][0]["fields"].append({"name": "new_field", "data_type": "text"})
    result = run(state)
    assert result["status"] == "blocked" and result["reason_code"] == "source_schema_changed"
    assert result["projected_sequence"] == 0 and data_counts(state)["entities"] == 0


def test_complete_unit_over_budget_remains_received_without_partial_publication(platform, monkeypatch):
    state = setup_projection(platform, monkeypatch)
    append_events(state, fixtures()[:6])
    monkeypatch.setattr("ontology.cdc.projection.MAX_EVENTS", 3)
    result = run(state)
    assert result["status"] == "budget_exceeded" and result["projected_sequence"] == 0
    assert result["received_sequence"] == 6 and result["baseline"] is None
    assert data_counts(state)["source_record_revisions"] == 0


def test_complete_baseline_withdraws_absent_old_source_contribution_but_retains_manual_fields(
    platform, monkeypatch
):
    state = setup_projection(platform, monkeypatch)
    client, app, owner, _ = platform
    with app.state.db.transaction(owner["tenant_id"]) as session:
        snapshot = SourceSnapshot(
            tenant_id=owner["tenant_id"],
            project_id=owner["project_id"],
            source_id=state["source_id"],
            source_revision=1,
            resource_id="public.items",
            generation=1,
            schema_fingerprint=schema_fingerprint(state["resources"][0]),
            release_id=state["release"],
            status="complete",
            attempt=1,
            record_count=1,
            consistency="transaction",
        )
        session.add(snapshot)
        session.flush()
        key, values = (
            {"id": 99},
            {"id": 99, "name": "Old snapshot object", "amount": "10", "raw": None, "optional": None},
        )
        typed = canonical_identity_key(key)
        key_hash = fingerprint({"codec": "typed-v1", "key": typed})
        record = SourceRecordRevision(
            tenant_id=owner["tenant_id"],
            project_id=owner["project_id"],
            snapshot_id=snapshot.id,
            attempt=1,
            key_hash=key_hash,
            source_key=key,
            identity_key=typed,
            values=values,
            content_hash=fingerprint({"key": key, "identity_key": typed, "values": values}),
        )
        session.add(record)
        session.flush()
        entity = Entity(
            tenant_id=owner["tenant_id"],
            project_id=owner["project_id"],
            type_key="Item",
            release_id=state["release"],
            properties={
                "source_id": 99,
                "name": values["name"],
                "amount": "10",
                "optional": None,
                "note": "Keep this manual note",
            },
        )
        session.add(entity)
        session.flush()
        save_entity_revision(session, entity, session.get(User, owner["user_id"]))
        identity = SourceIdentity(
            tenant_id=owner["tenant_id"],
            project_id=owner["project_id"],
            source_id=state["source_id"],
            resource_id="public.items",
            key_hash=key_hash,
            key_codec="typed-v1",
            type_key="Item",
            entity_id=entity.id,
            last_record_id=record.id,
            last_record_codec=record.record_codec,
            mapping_key="items",
        )
        session.add(identity)
        entity_id = entity.id
    append_events(state, fixtures()[:5])
    assert run(state)["status"] == "waiting"
    assert client.get(f"/api/objects/{entity_id}").json()["properties"]["name"] == values["name"]
    append_events(state, fixtures()[5:6])
    result = run(state)
    assert result["status"] == "caught_up", result
    entity = client.get(f"/api/objects/{entity_id}").json()
    assert entity["data_status"] == "withdrawn" and not entity["deleted"]
    assert entity["properties"] == {"note": "Keep this manual note"}
    contributions = client.get(f"/api/objects/{entity_id}/contributions").json()
    assert contributions["items"][0]["withdraw_reason"] == "complete_baseline_absent"
    assert "native_row" not in contributions["items"][0]
    contribution_id = contributions["items"][0]["id"]
    history = client.get(f"/api/objects/{entity_id}/contributions/{contribution_id}/history").json()["items"]
    assert [row["active"] for row in history] == [False, True]
    assert history[0]["mapped_values"]["name"] == values["name"]


def test_authorization_revoked_during_catalog_network_io_prevents_commit(platform, monkeypatch):
    state = setup_projection(platform, monkeypatch)
    append_events(state, fixtures()[:6])
    _, app, owner, _ = platform

    class RevokingCatalog:
        def discover(self):
            with app.state.db.transaction(owner["tenant_id"]) as session:
                member = session.scalar(
                    select(Membership).where(
                        Membership.user_id == owner["user_id"], Membership.project_id == owner["project_id"]
                    )
                )
                member.role = "member"
            return [ResourceSchema.model_validate(r) for r in state["resources"]]

        def close(self):
            pass

    monkeypatch.setattr("ontology.cdc.projection.connector_for", lambda *_: RevokingCatalog())
    response = platform[0].post(
        f"/api/cdc/streams/{state['stream']['id']}/project", json={"expected_binding_id": state["binding_id"]}
    )
    assert response.status_code == 202
    assert process_one(app.state.db, app.state.settings, "revocation-test")
    with app.state.db.transaction(owner["tenant_id"]) as session:
        assert session.get(CdcStream, state["stream"]["id"]).projected_sequence == 0
        assert session.get(Job, response.json()["job"]["id"]).status == "failed"
        assert (
            session.scalar(
                select(CdcProjectionState).where(CdcProjectionState.stream_id == state["stream"]["id"])
            ).status
            == "blocked"
        )
    assert data_counts(state)["entities"] == 0


@pytest.mark.parametrize("fixture_name", ["platform", "pg_platform"])
def test_actual_native_delete_and_tombstone_withdraw_only_once(request, fixture_name, monkeypatch):
    state = setup_projection(request.getfixturevalue(fixture_name), monkeypatch)
    native = fixtures("pg-delete-events.json")
    append_events(state, native)
    for _ in range(4):
        result = run(state, automatic=True)
        assert result["status"] == "waiting", result
    client, app, owner, _ = state["platform"]
    with app.state.db.transaction(owner["tenant_id"]) as session:
        entities = list(session.scalars(select(Entity).where(Entity.project_id == owner["project_id"])))
        entity = next(e for e in entities if e.properties["source_id"] == 1)
        entity_id = entity.id
        previous_revision = entity.revision
    response = client.patch(
        f"/api/objects/{entity_id}",
        json={
            "expected_revision": previous_revision,
            "expected_model_release_id": state["release"],
            "properties": {"note": "Retain manual content"},
        },
    )
    assert response.status_code == 200, response.text
    committed = run(state, automatic=True)
    assert committed["status"] == "caught_up" and committed["projected_sequence"] == len(native) == 19, (
        committed
    )
    entity = client.get(f"/api/objects/{entity_id}").json()
    assert entity["data_status"] == "withdrawn" and entity["properties"] == {"note": "Retain manual content"}
    assert entity["revision"] == previous_revision + 2 and not entity["deleted"]
    rows = client.get(f"/api/objects/{entity_id}/contributions").json()["items"]
    assert len(rows) == 1 and rows[0]["withdraw_reason"] == "native_delete"
    history = client.get(f"/api/objects/{entity_id}/contributions/{rows[0]['id']}/history").json()["items"]
    assert len(history) == 5 and sum(not row["active"] for row in history) == 1
    assert committed["last_job"]["result"]["withdrawn"] == 1
    assert data_counts(state)["source_record_revisions"] == 6  # no new source reference for tombstone
    before = data_counts(state)
    assert append_events(state, native)["inserted"] == 0
    assert not schedule_pending(app.state.db, owner["tenant_id"])
    assert data_counts(state) == before


def test_source_contribution_current_and_history_keep_current_field_permissions(platform, monkeypatch):
    state = setup_projection(platform, monkeypatch, private_amount=True)
    append_events(state, fixtures()[:6])
    assert run(state)["status"] == "caught_up"
    client, app, owner, _ = platform
    with app.state.db.transaction(owner["tenant_id"]) as session:
        entity_id = session.scalar(
            select(Entity.id).where(Entity.project_id == owner["project_id"]).order_by(Entity.created_at)
        )
    user = client.post(
        "/api/users",
        json={
            "email": "source-reader@example.test",
            "display_name": "Source reader",
            "password": "Source-Reader-Password-2026",
        },
    ).json()
    client.post(
        "/api/members", json={"project_id": owner["project_id"], "user_id": user["id"], "role": "modeler"}
    )
    with TestClient(app) as reader:
        assert (
            reader.post(
                "/api/auth/login",
                json={
                    "tenant_slug": "alpha",
                    "email": "source-reader@example.test",
                    "password": "Source-Reader-Password-2026",
                },
            ).status_code
            == 200
        )
        response = reader.get(f"/api/objects/{entity_id}/contributions")
        assert response.status_code == 200, response.text
        row = response.json()["items"][0]
        assert "amount" not in row["mapped_values"] and "native_row" not in row
        assert "123456789012" not in response.text
        history = reader.get(f"/api/objects/{entity_id}/contributions/{row['id']}/history")
        assert history.status_code == 200 and "123456789012" not in history.text
        client.post(
            "/api/members", json={"project_id": owner["project_id"], "user_id": user["id"], "role": "member"}
        )
        assert reader.get(f"/api/objects/{entity_id}/contributions").status_code == 403
        assert reader.get(f"/api/objects/{entity_id}/contributions/{row['id']}/history").status_code == 403
    with app.state.db.transaction(owner["tenant_id"]) as session:
        session.get(Entity, entity_id).deleted = True
    assert client.get(f"/api/objects/{entity_id}/contributions").status_code == 404


def test_one_source_field_can_populate_multiple_properties(platform, monkeypatch):
    state = setup_projection(platform, monkeypatch, duplicate_source_mapping=True)
    append_events(state, fixtures()[:6])
    assert run(state)["status"] == "caught_up"
    with platform[1].state.db.transaction(platform[2]["tenant_id"]) as session:
        entities = list(session.scalars(select(Entity).where(Entity.project_id == platform[2]["project_id"])))
        assert entities and all(
            entity.properties["name"] == entity.properties["alias"] for entity in entities
        )


def test_pausing_an_already_queued_projection_can_resume_the_same_reviewed_binding(platform, monkeypatch):
    state = setup_projection(platform, monkeypatch)
    append_events(state, fixtures()[:6])
    client, app, _owner, _ = platform
    assert (
        client.post(
            f"/api/cdc/streams/{state['stream']['id']}/project",
            json={"expected_binding_id": state["binding_id"]},
        ).status_code
        == 202
    )
    assert client.post(f"/api/cdc/streams/{state['stream']['id']}/pause").status_code == 200
    assert process_one(app.state.db, app.state.settings, "paused-worker")
    assert view(state)["status"] == "blocked" and view(state)["projected_sequence"] == 0
    assert client.post(f"/api/cdc/streams/{state['stream']['id']}/resume").status_code == 200
    result = run(state, automatic=True)
    assert result["status"] == "caught_up" and result["projected_sequence"] == 6
    assert result["binding"]["id"] == state["binding_id"]


@pytest.mark.parametrize(
    "scenario,empty_resource,expected_objects", [("empty", False, 0), ("multi", True, 2)]
)
def test_actual_empty_and_multiple_resource_snapshot_proof_commits(
    platform, monkeypatch, scenario, empty_resource, expected_objects
):
    state = setup_projection(platform, monkeypatch, empty=empty_resource)
    captured = json.loads(
        (ROOT / "integrations/debezium/protocol/pg-snapshot-events.json").read_text(encoding="utf-8")
    )
    native = captured["scenarios"][scenario]["events"]
    append_events(state, native)
    result = run(state)
    assert result["status"] == "caught_up", result
    assert result["baseline"]["end_sequence"] == len(native)
    assert data_counts(state)["entities"] == expected_objects
    assert data_counts(state)["cdc_projection_windows"] == 1
