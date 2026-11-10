"""Opt-in full source/WAL/discovery/mapping/worker/object lifecycle.

The source is real PostgreSQL and the pinned Debezium sidecar delivers real WAL.
The application store is the fixture's disposable SQLite database; PostgreSQL
application RLS/transactions have separate integration tests. No catalog, event,
mapping result or worker is mocked. The fixture owns cleanup by its unique labels.
"""

import json
import os
from time import monotonic

import pytest
from ontology.cdc.models import CdcStream
from ontology.objects.contributions.models import ContributionRevision, SourceContribution
from ontology.objects.models import Entity, EntityRevision
from ontology.sources.models import SourceIdentity, SourceRecordRevision
from ontology.worker import run_worker
from sqlalchemy import func, select

from tests.support.live_cdc import data_events, eventually, snapshot_notifications
from tests.support.live_cdc import live_cdc as live_cdc  # noqa: PLC0414 - pytest fixture registration
from tests.support.live_connectors import execute
from tests.support.platform import publish

pytestmark = pytest.mark.skipif(
    os.environ.get("ONTOLOGY_LIVE_CDC") != "1", reason="Explicit live CDC opt-in required"
)
PINNED_ADAPTER = "sha256:bdc5f514abdcc20488d889be957a44ddf67dd8da628a5a3937c9574ea2f2411b"


def projection(state):
    result = state["client"].get(f"/api/cdc/streams/{state['stream']['id']}/projection")
    assert result.status_code == 200, result.text
    return result.json()


def drain(state, target_sequence):
    """Use the public worker entry, including scheduling and job fencing."""

    def tick():
        run_worker(state["app"].state.settings, once=True)
        result = projection(state)
        assert result["status"] not in {"blocked", "failed", "budget_exceeded"}, result
        assert result["fulltext"]["status"] != "failed", result["fulltext"]
        caught_up = (
            result["projected_sequence"] >= target_sequence
            and result["fulltext"]["sequence"] >= result["projected_sequence"]
            and result["fulltext"]["status"] == "ready"
        )
        return result if caught_up else None

    return eventually(
        tick, timeout=45, description="formal worker committed objects and object full-text checkpoint"
    )


def received_sequence(state):
    with state["app"].state.db.transaction(state["account"]["tenant_id"]) as session:
        return session.get(CdcStream, state["stream"]["id"]).received_sequence


def wait_transaction_end(state, event):
    txid = event["source_offset"]["txId"]
    return eventually(
        lambda: [
            row
            for row in state["events"]()
            if row["event_kind"] == "transaction"
            and row["transaction"]["status"] == "END"
            and row["source_offset"].get("txId") == txid
        ],
        description="native transaction END in durable inbox",
    )[-1]


def object_view(state, object_id):
    result = state["client"].get(f"/api/objects/{object_id}")
    assert result.status_code == 200, result.text
    return result.json()


def search_objects(state, query):
    client, project_id = state["client"], state["account"]["project_id"]
    model = client.get("/api/ontology/definition", params={"project_id": project_id})
    assert model.status_code == 200, model.text
    response = client.post(
        "/api/search/objects",
        json={
            "project_id": project_id,
            "q": query,
            "expected_release_id": model.json()["release_id"],
            "expected_model_version": model.json()["version"],
        },
    )
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["index"]["status"] == "ready", result["index"]
    return {
        "query": result["query"],
        "total": result["total"],
        "object_ids": [row["id"] for row in result["items"]],
        "index": result["index"],
        "matches": result["matches"],
    }


def ledger_counts(state):
    with state["app"].state.db.transaction(state["account"]["tenant_id"]) as session:
        return {
            model.__tablename__: session.scalar(select(func.count()).select_from(model))
            for model in (
                Entity,
                EntityRevision,
                SourceIdentity,
                SourceRecordRevision,
                SourceContribution,
                ContributionRevision,
            )
        }


def test_real_source_discovery_mapping_worker_update_delete_and_tombstone(request):
    started = monotonic()
    state = request.getfixturevalue("live_cdc")
    client = state["client"]
    runtime = json.loads((state["directory"] / "result-context.json").read_text(encoding="utf-8"))
    assert runtime["adapter_image_id"] == PINNED_ADAPTER
    eventually(
        lambda: snapshot_notifications(state, "COMPLETED"),
        timeout=60,
        description="real native initial snapshot completion",
    )
    source_id = state["stream"]["source_id"]
    tested = client.post(f"/api/sources/{source_id}/test")
    assert tested.status_code == 200 and tested.json()["ok"], tested.text
    # This endpoint opens SQLConnector against the real source with its encrypted
    # credential. Every projection run also discovers through that same connector.
    discovered = client.post(f"/api/sources/{source_id}/discover")
    assert discovered.status_code == 200, discovered.text
    resource = next(row for row in discovered.json()["resources"] if row["resource_id"] == "public.items")
    assert resource["primary_key"] == ["id"]
    assert {field["name"] for field in resource["fields"]} == {"id", "name", "amount", "raw", "optional"}
    definition = {
        "types": [
            {
                "key": "LiveItem",
                "label": "真实 CDC 项目",
                "properties": [
                    {"key": "source_id", "label": "来源主键", "data_type": "integer", "source_owned": True},
                    {
                        "key": "name",
                        "label": "名称",
                        "data_type": "string",
                        "source_owned": True,
                        "required": True,
                    },
                    {"key": "amount", "label": "金额", "data_type": "number", "source_owned": True},
                    {"key": "optional", "label": "可空字段", "data_type": "string", "source_owned": True},
                    {"key": "note", "label": "人工备注", "data_type": "string"},
                ],
            }
        ],
        "mappings": [
            {
                "key": "live_items",
                "source_id": source_id,
                "resource_id": resource["resource_id"],
                "type_key": "LiveItem",
                "schema_fingerprint": resource["schema_fingerprint"],
                "fields": {
                    "source_id": {"source_field": "id"},
                    "name": {"source_field": "name"},
                    "amount": {"source_field": "amount"},
                    "optional": {"source_field": "optional"},
                },
            }
        ],
    }
    published = publish(client, state["account"]["project_id"], definition)
    configured = client.post(
        f"/api/cdc/streams/{state['stream']['id']}/projection",
        json={
            "expected_release_id": published["release_id"],
            "expected_source_revision": discovered.json()["source"]["revision"],
            "resource_ids": [resource["resource_id"]],
        },
    )
    assert configured.status_code == 200, configured.text
    initial = drain(state, received_sequence(state))
    assert initial["baseline"]["proof_kind"] == "completed_notification" and initial["indexed_sequence"] == 0
    listed = client.get(
        "/api/objects", params={"project_id": state["account"]["project_id"], "type_key": "LiveItem"}
    )
    assert listed.status_code == 200, listed.text
    objects = {row["properties"]["source_id"]: row for row in listed.json()["items"]}
    assert set(objects) == {1, 2}
    first, retained = objects[1], objects[2]
    assert first["properties"]["amount"] == "123456789012.123456789012345678"
    assert first["properties"]["optional"] is None and first["revision"] == 1
    initial_search = search_objects(state, first["properties"]["name"])
    assert initial_search["total"] == 1 and initial_search["object_ids"] == [first["id"]]
    manual = client.patch(
        f"/api/objects/{first['id']}",
        json={
            "expected_revision": first["revision"],
            "expected_model_release_id": first["model_release_id"],
            "properties": {"note": "人工字段保留"},
        },
    )
    assert manual.status_code == 200, manual.text

    execute(state["conn"], "UPDATE items SET name='真实更新', amount=7.123456789012345678 WHERE id=1")
    updated_event = eventually(
        lambda: [
            row
            for row in data_events(state, "u")
            if row["value"]["payload"]["after"].get("name") == "真实更新"
        ],
        description="real UPDATE in inbox",
    )[-1]
    update_end = wait_transaction_end(state, updated_event)
    updated_progress = drain(state, received_sequence(state))
    updated = object_view(state, first["id"])
    assert updated["properties"] == {
        "source_id": 1,
        "name": "真实更新",
        "amount": "7.123456789012345678",
        "optional": None,
        "note": "人工字段保留",
    }
    assert updated["data_status"] == "active" and updated["revision"] == manual.json()["revision"] + 1
    assert object_view(state, retained["id"])["revision"] == retained["revision"]
    updated_search = search_objects(state, "真实更新")
    manual_search = search_objects(state, "人工字段保留")
    assert updated_search["total"] == 1 and updated_search["object_ids"] == [first["id"]]
    assert manual_search["total"] == 1 and manual_search["object_ids"] == [first["id"]]
    assert updated_progress["indexed_sequence"] == 0

    execute(state["conn"], "DELETE FROM items WHERE id=1")
    deleted = eventually(
        lambda: [row for row in data_events(state, "d") if row["key"]["payload"]["id"] == 1],
        description="real DELETE in inbox",
    )[-1]
    delete_end = wait_transaction_end(state, deleted)
    tombstones = [
        row
        for row in state["events"]()
        if row["event_kind"] == "tombstone"
        and row["key"]["payload"]["id"] == 1
        and row["source_offset"].get("txId") == deleted["source_offset"]["txId"]
    ]
    assert len(tombstones) == 1 and delete_end["transaction"]["event_count"] == 1
    final = drain(state, received_sequence(state))
    withdrawn = object_view(state, first["id"])
    assert withdrawn["data_status"] == "withdrawn" and not withdrawn["deleted"]
    assert withdrawn["properties"] == {"note": "人工字段保留"}
    assert withdrawn["revision"] == updated["revision"] + 1
    visible = client.get(
        "/api/objects", params={"project_id": state["account"]["project_id"], "type_key": "LiveItem"}
    ).json()["items"]
    assert [row["id"] for row in visible] == [retained["id"]]
    deleted_search = search_objects(state, "真实更新")
    withdrawn_manual_search = search_objects(state, "人工字段保留")
    assert deleted_search["total"] == 0 and deleted_search["object_ids"] == []
    assert withdrawn_manual_search["total"] == 0 and withdrawn_manual_search["object_ids"] == []
    current = client.get(f"/api/objects/{first['id']}/contributions").json()["items"]
    assert (
        len(current) == 1
        and current[0]["active"] is False
        and current[0]["withdraw_reason"] == "native_delete"
    )
    assert current[0]["revision"] == 3 and current[0]["mapped_values"]["name"] == "真实更新"
    history = client.get(f"/api/objects/{first['id']}/contributions/{current[0]['id']}/history").json()[
        "items"
    ]
    assert len(history) == 3 and sum(row["active"] is False for row in history) == 1
    assert all("native_row" not in row for row in [*current, *history])
    assert (
        final["projected_sequence"] > updated_progress["projected_sequence"] > initial["projected_sequence"]
    )
    assert final["indexed_sequence"] == 0
    before = ledger_counts(state)
    run_worker(state["app"].state.settings, once=True)
    run_worker(state["app"].state.settings, once=True)
    assert ledger_counts(state) == before
    assert object_view(state, first["id"])["revision"] == withdrawn["revision"]
    with state["app"].state.db.transaction(state["account"]["tenant_id"]) as session:
        records = session.scalars(select(SourceRecordRevision)).all()
        assert all(
            row.record_codec == "cdc-native-reference-v1" and row.snapshot_id is None and row.values is None
            for row in records
        )
    (state["directory"] / "projection-result.json").write_text(
        json.dumps(
            {
                "description": "Real source PostgreSQL -> pinned Debezium -> Python inbox -> actual SQLConnector discovery -> published mapping -> formal worker -> objects -> object full-text index/checkpoint -> search API",
                "runtime": runtime,
                "source_schema": resource,
                "mapping": definition,
                "initial_progress": initial,
                "updated_progress": updated_progress,
                "final_progress": final,
                "update_end_event_id": update_end["event_id"],
                "delete_end_event_id": delete_end["event_id"],
                "tombstone_count": len(tombstones),
                "withdrawn_object": withdrawn,
                "contribution_history": history,
                "stable_after_repeated_worker_ticks": True,
                "application_database": "disposable SQLite (not a PostgreSQL application-store test)",
                "fulltext_verification": {
                    "initial": initial_search,
                    "updated": updated_search,
                    "manual_before_delete": manual_search,
                    "deleted": deleted_search,
                    "manual_after_withdrawal": withdrawn_manual_search,
                    "checkpoints": [initial["fulltext"], updated_progress["fulltext"], final["fulltext"]],
                    "comprehensive_index_sequence_unchanged": final["indexed_sequence"] == 0,
                },
                "elapsed_seconds_before_fixture_cleanup": round(monotonic() - started, 3),
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
