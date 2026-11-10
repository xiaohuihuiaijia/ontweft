"""Captured native CDC -> durable inbox -> real projection -> object full text.

No new source service is started here. These tests replay the unchanged native
PostgreSQL capture; catalog discovery is the existing projection test fixture.
Both SQLite and a disposable restricted-role PostgreSQL app database are used.
"""

import pytest
from ontology.cdc.models import CdcStream
from ontology.objects.models import Entity
from ontology.search.objects.index import process_one_index
from ontology.search.objects.models import (
    ObjectSearchDocument,
    ObjectSearchField,
    ObjectSearchRequest,
    SearchCdcCheckpoint,
    SearchProjectState,
)
from ontology.search.tokens import SearchBudgetError
from sqlalchemy import func, select

from tests.support.cdc_projection import append_events, fixtures, run, setup_projection, view
from tests.support.postgres import pg_databases as pg_databases  # noqa: PLC0414 - pytest fixture registration
from tests.support.postgres import pg_platform as pg_platform  # noqa: PLC0414 - pytest fixture registration


@pytest.fixture(params=["platform", "pg_platform"])
def indexed_cdc(request, monkeypatch):
    state = setup_projection(request.getfixturevalue(request.param), monkeypatch)
    native = fixtures("pg-delete-events.json")
    append_events(state, native[:6])
    assert run(state, automatic=True)["projected_sequence"] == 6
    _, app, owner, _ = state["platform"]
    with app.state.db.transaction(owner["tenant_id"]) as session:
        entity = next(
            e
            for e in session.scalars(select(Entity).where(Entity.project_id == owner["project_id"]))
            if e.properties["source_id"] == 1
        )
        state["entity_id"] = entity.id
    state["native"] = native
    return state


def find(state, query):
    client, _, owner, _ = state["platform"]
    model = client.get("/api/ontology/definition", params={"project_id": owner["project_id"]}).json()
    response = client.post(
        "/api/search/objects",
        json={
            "project_id": owner["project_id"],
            "expected_release_id": model["release_id"],
            "expected_model_version": model["version"],
            "q": query,
        },
    )
    assert response.status_code == 200, response.text
    return response.json()


def index_once(state, batch_size=10):
    _, app, owner, _ = state["platform"]
    return process_one_index(app.state.db, owner["tenant_id"], batch_size=batch_size)


def drain(state):
    for _ in range(20):
        if not index_once(state):
            return
    pytest.fail("Bounded synthetic index did not finish")


def test_native_update_and_delete_remove_stale_search_hits_before_index_catches_up(indexed_cdc):
    state = indexed_cdc
    client, app, owner, _ = state["platform"]
    native, entity_id = state["native"], state["entity_id"]
    initial_name = native[2]["value"]["payload"]["after"]["name"]
    final_name = native[13]["value"]["payload"]["after"]["name"]
    before = find(state, initial_name)
    assert before["total"] == 0 and before["index"]["status"] == "not_ready"
    assert view(state)["fulltext"]["sequence"] == 0
    drain(state)
    assert [row["id"] for row in find(state, initial_name)["items"]] == [entity_id]
    indexed = view(state)
    assert indexed["fulltext"]["sequence"] == 6 and indexed["fulltext"]["status"] == "ready"
    assert indexed["indexed_sequence"] == 0

    # Apply all three actual updates, including unchanged TOAST and explicit
    # NULL clearing. Coalescing indexes the newest complete object revision.
    append_events(state, native[6:15])
    for _ in range(3):
        run(state, automatic=True)
    assert view(state)["projected_sequence"] == 15
    assert view(state)["fulltext"]["sequence"] == 6
    assert find(state, initial_name)["total"] == 0  # old receipt is immediately ineligible
    assert find(state, final_name)["total"] == 0
    with app.state.db.transaction(owner["tenant_id"]) as session:
        request = session.scalar(
            select(ObjectSearchRequest).where(ObjectSearchRequest.entity_id == entity_id)
        )
        entity = session.get(Entity, entity_id)
        assert request.requested_revision == entity.revision == 4
    drain(state)
    assert [row["id"] for row in find(state, final_name)["items"]] == [entity_id]
    assert view(state)["fulltext"]["sequence"] == 15

    current = client.get(f"/api/objects/{entity_id}").json()
    updated = client.patch(
        f"/api/objects/{entity_id}",
        json={
            "expected_revision": current["revision"],
            "expected_model_release_id": state["release"],
            "properties": {"note": "Retain manual content"},
        },
    )
    assert updated.status_code == 200, updated.text
    drain(state)
    append_events(state, native[15:])
    withdrawn = run(state, automatic=True)
    assert withdrawn["projected_sequence"] == 19 and withdrawn["fulltext"]["sequence"] == 15
    assert find(state, final_name)["total"] == 0  # status also invalidates a stale ready receipt
    assert find(state, "Retain")["total"] == 0
    drain(state)
    done = view(state)
    assert done["fulltext"]["sequence"] == 19 and done["fulltext"]["status"] == "ready"
    assert done["indexed_sequence"] == 0
    with app.state.db.transaction(owner["tenant_id"]) as session:
        entity = session.get(Entity, entity_id)
        assert entity.data_status == "withdrawn" and not entity.deleted
        assert entity.properties == {"note": "Retain manual content"}
        receipt = session.scalar(
            select(ObjectSearchDocument).where(ObjectSearchDocument.entity_id == entity_id)
        )
        assert receipt.status == "removed" and receipt.entity_revision == entity.revision
        assert (
            session.scalar(
                select(func.count())
                .select_from(ObjectSearchField)
                .where(ObjectSearchField.entity_id == entity_id)
            )
            == 0
        )
        assert (
            session.scalar(select(ObjectSearchRequest.id).where(ObjectSearchRequest.entity_id == entity_id))
            is None
        )
    assert append_events(state, native)["inserted"] == 0
    assert not index_once(state)


def test_one_failed_object_blocks_snapshot_checkpoint_until_explicit_rebuild(indexed_cdc, monkeypatch):
    import ontology.search.objects.index as search_index

    state = indexed_cdc
    client, app, owner, _ = state["platform"]
    original = search_index._field_chunks

    def failure(entity, definition):
        if entity.id == state["entity_id"]:
            raise SearchBudgetError("Synthetic bounded object failure")
        return original(entity, definition)

    monkeypatch.setattr(search_index, "_field_chunks", failure)
    drain(state)
    failed = view(state)
    assert failed["fulltext"]["status"] == "failed" and failed["fulltext"]["sequence"] == 0
    with app.state.db.transaction(owner["tenant_id"]) as session:
        receipts = list(
            session.scalars(
                select(ObjectSearchDocument).where(ObjectSearchDocument.project_id == owner["project_id"])
            )
        )
        assert sorted(row.status for row in receipts) == ["error", "ready"]
        old_generation = receipts[0].generation
        assert session.scalar(select(SearchCdcCheckpoint.sequence)) == 0
        request = session.scalar(
            select(ObjectSearchRequest).where(ObjectSearchRequest.entity_id == state["entity_id"])
        )
        assert request.error == "Synthetic bounded object failure"
    monkeypatch.setattr(search_index, "_field_chunks", original)
    response = client.post(
        "/api/search/rebuild",
        json={
            "project_id": owner["project_id"],
            "expected_release_id": state["release"],
            "expected_model_version": 1,
        },
    )
    assert response.status_code == 202, response.text
    assert response.json()["generation"] != old_generation
    other_name = state["native"][4]["value"]["payload"]["after"]["name"]
    assert find(state, other_name)["total"] == 0  # old ready generation is not served
    # A two-object snapshot must not advance when only the first is rebuilt.
    assert index_once(state, batch_size=1)
    assert view(state)["fulltext"]["sequence"] == 0
    drain(state)
    assert view(state)["fulltext"]["sequence"] == 6
    assert view(state)["fulltext"]["status"] == "ready"


def test_unexpected_index_failure_rolls_back_fields_receipts_queue_and_checkpoint(indexed_cdc, monkeypatch):
    import ontology.search.objects.index as search_index

    state = indexed_cdc
    _, app, owner, _ = state["platform"]
    original = search_index._advance_checkpoints

    def fail_after_checkpoint(session, project, project_state):
        original(session, project, project_state)
        session.flush()
        raise RuntimeError("Synthetic failure after staged checkpoint")

    monkeypatch.setattr(search_index, "_advance_checkpoints", fail_after_checkpoint)
    assert not index_once(state)
    with app.state.db.transaction(owner["tenant_id"]) as session:
        for model in (ObjectSearchDocument, ObjectSearchField, SearchCdcCheckpoint):
            assert (
                session.scalar(
                    select(func.count()).select_from(model).where(model.project_id == owner["project_id"])
                )
                == 0
            )
        assert (
            session.scalar(
                select(func.count())
                .select_from(ObjectSearchRequest)
                .where(ObjectSearchRequest.project_id == owner["project_id"])
            )
            == 2
        )
        status = session.scalar(
            select(SearchProjectState).where(SearchProjectState.project_id == owner["project_id"])
        )
        assert status.error is not None and not status.backfill_complete
        stream = session.get(CdcStream, state["stream"]["id"])
        assert stream.projected_sequence == 6 and stream.indexed_sequence == 0
    monkeypatch.setattr(search_index, "_advance_checkpoints", original)
    assert not index_once(state)  # persisted unexpected failure is not silently retried


def test_old_failed_batch_cannot_cancel_a_new_revision_retry_between_transactions(indexed_cdc, monkeypatch):
    import ontology.search.objects.index as search_index

    state = indexed_cdc
    client, app, owner, _ = state["platform"]
    drain(state)

    def edit(note):
        entity = client.get(f"/api/objects/{state['entity_id']}").json()
        response = client.patch(
            f"/api/objects/{state['entity_id']}",
            json={
                "expected_revision": entity["revision"],
                "expected_model_release_id": state["release"],
                "properties": {"note": note},
            },
        )
        assert response.status_code == 200, response.text
        return response.json()["revision"]

    assert edit("First pending revision") == 2
    original = search_index._field_chunks
    intervened = []

    def fail_old_batch(*_):
        raise RuntimeError("Synthetic failed older batch")

    def commit_new_revision_after_rollback(*_):
        # The error is logged after the failed transaction has exited and before
        # its separate diagnostic transaction takes the project lock again.
        intervened.append(edit("Newer revision requests retry"))

    with monkeypatch.context() as patch:
        patch.setattr(search_index, "_field_chunks", fail_old_batch)
        patch.setattr(search_index.logger, "error", commit_new_revision_after_rollback)
        assert not index_once(state)
    assert intervened == [3]
    with app.state.db.transaction(owner["tenant_id"]) as session:
        project_state = session.scalar(
            select(SearchProjectState).where(SearchProjectState.project_id == owner["project_id"])
        )
        pending = session.scalar(
            select(ObjectSearchRequest).where(ObjectSearchRequest.entity_id == state["entity_id"])
        )
        assert project_state.error is None, "An old failure suppressed a newer committed retry request"
        assert pending.requested_revision == 3 and pending.error is None
    assert search_index._field_chunks is original
    drain(state)
    assert [item["id"] for item in find(state, "Newer")["items"]] == [state["entity_id"]]
