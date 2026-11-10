"""Real PostgreSQL WAL -> Debezium -> real Python inbox; explicit Docker opt-in.

ONTOLOGY_LIVE_CDC=1 pytest backend/tests/live/test_live_cdc.py -q --tb=short
Build integrations/debezium first. Only uniquely labelled test containers are removed.
The receiver uses the real application with a disposable SQLite inbox in this test;
PostgreSQL inbox locking/RLS is covered separately by test_postgres.py.
"""

from __future__ import annotations

import hashlib
import json
import os

import pytest
from ontology.cdc.decode import ValueState, decode_pg_event
from ontology.cdc.models import CdcStream
from ontology.cdc.values import RowBinding, decode_row, encode_row, mapping_values, merge_row
from ontology.cdc.windows import next_cdc_window, schema_fingerprint

from tests.support.live_cdc import (
    assert_one_event_chunks_replay_after_durable_prefix_and_rejected_suffix,
    assert_restart_after_commit_before_ack_deduplicates_same_source_event,
    assert_snapshot_preserves_keys_schema_precision_nulls_and_offset,
    assert_transaction_same_key_updates_pk_change_delete_and_tombstone,
    assert_unacked_batch_does_not_advance_offset_schema_and_truncate_are_visible,
    data_events,
    eventually,
    snapshot_notifications,
)
from tests.support.live_cdc import live_cdc as live_cdc  # noqa: PLC0414 - pytest fixture registration
from tests.support.live_connectors import execute

pytestmark = pytest.mark.skipif(
    os.environ.get("ONTOLOGY_LIVE_CDC") != "1", reason="Explicit live CDC opt-in required"
)


def test_real_postgres_cdc_snapshot_transactions_restart_schema_and_truncate(live_cdc):
    # One explicit lifecycle, so selecting/reordering tests cannot omit prerequisites.
    assert_snapshot_preserves_keys_schema_precision_nulls_and_offset(live_cdc)
    assert_transaction_same_key_updates_pk_change_delete_and_tombstone(live_cdc)
    assert_restart_after_commit_before_ack_deduplicates_same_source_event(live_cdc)
    assert_one_event_chunks_replay_after_durable_prefix_and_rejected_suffix(live_cdc)
    assert_unacked_batch_does_not_advance_offset_schema_and_truncate_are_visible(live_cdc)


def test_real_unchanged_toast_restores_persisted_value_then_explicit_null_clears(live_cdc):
    state = live_cdc
    assert_snapshot_preserves_keys_schema_precision_nulls_and_offset(state)
    eventually(lambda: snapshot_notifications(state, "COMPLETED"))
    # FULL replica identity sends old TOAST values. DEFAULT deliberately exercises
    # the actual pgoutput unchanged-TOAST representation, not a synthetic marker.
    execute(state["conn"], "ALTER TABLE items REPLICA IDENTITY DEFAULT")
    execute(state["conn"], "ALTER TABLE items ALTER COLUMN optional SET STORAGE EXTERNAL")
    execute(
        state["conn"],
        "UPDATE items SET optional=repeat('ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789',1000), name='TOAST完整值' WHERE id=1",
    )
    expected_text = "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789" * 1000

    def named_update(name):
        matches = [
            event
            for event in data_events(state, "u")
            if event["value"]["payload"]["after"].get("name") == name
        ]
        return matches[-1] if matches else None

    full_event = eventually(lambda: named_update("TOAST完整值"))
    toast_bytes = execute(
        state["conn"], "SELECT pg_relation_size(reltoastrelid) FROM pg_class WHERE oid='items'::regclass"
    )[0][0]
    assert toast_bytes > 0
    full = decode_pg_event(full_event)
    assert full.after.fields["optional"].value == expected_text

    def bound(event):
        change = decode_pg_event(event)
        return RowBinding(
            change.resource_id, schema_fingerprint(event), change.identity_key, state["stream"]["generation"]
        )

    saved = json.loads(json.dumps(encode_row(full.after)))
    execute(state["conn"], "UPDATE items SET name='TOAST未变' WHERE id=1")
    unchanged_event = eventually(lambda: named_update("TOAST未变"))
    assert unchanged_event["value"]["payload"]["after"]["optional"] == "__debezium_unavailable_value"
    unchanged = decode_pg_event(unchanged_event)
    assert unchanged.after.fields["optional"].state == ValueState.UNAVAILABLE
    restored = merge_row(
        unchanged.after,
        operation="u",
        binding=bound(unchanged_event),
        previous=decode_row(saved),
        previous_binding=bound(full_event),
    )
    assert restored.fields["optional"].value == expected_text
    assert (
        mapping_values(restored, fields=["id", "name", "amount"])["amount"]
        == "123456789012.123456789012345678"
    )
    saved = json.loads(json.dumps(encode_row(restored)))

    execute(state["conn"], "UPDATE items SET optional=NULL, name='TOAST清空' WHERE id=1")
    cleared_event = eventually(lambda: named_update("TOAST清空"))
    cleared = decode_pg_event(cleared_event)
    assert cleared.after.fields["optional"].state == ValueState.NULL
    restored = merge_row(
        cleared.after,
        operation="u",
        binding=bound(cleared_event),
        previous=decode_row(saved),
        previous_binding=bound(unchanged_event),
    )
    assert mapping_values(restored, fields=["id", "name", "optional"])["optional"] is None
    eventually(
        lambda: [
            event
            for event in state["events"]()
            if event["event_kind"] == "transaction"
            and event["transaction"]["status"] == "END"
            and event["source_offset"].get("txId") == cleared_event["source_offset"]["txId"]
        ]
    )
    (state["directory"] / "toast-events.json").write_text(
        json.dumps(
            {
                "description": "Actual pgoutput unchanged TOAST and explicit NULL; exclusively self-owned synthetic data",
                "runtime": json.loads(
                    (state["directory"] / "result-context.json").read_text(encoding="utf-8")
                ),
                "generation": state["stream"]["generation"],
                "replica_identity": "DEFAULT",
                "optional_storage": "EXTERNAL",
                "toast_relation_bytes": toast_bytes,
                "text_length": len(expected_text),
                "text_sha256": hashlib.sha256(expected_text.encode()).hexdigest(),
                "events": state["events"](),
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    # Retain a complete native delete transaction for business-withdrawal tests.
    # The earlier TOAST capture stays unchanged; do not substitute a tombstone
    # sample for the actual DELETE, native boundaries and END count.
    execute(state["conn"], "DELETE FROM items WHERE id=1")
    deleted = eventually(
        lambda: [event for event in data_events(state, "d") if event["key"]["payload"]["id"] == 1]
    )[-1]
    txid = deleted["source_offset"]["txId"]
    ended = eventually(
        lambda: [
            event
            for event in state["events"]()
            if event["event_kind"] == "transaction"
            and event["transaction"]["status"] == "END"
            and event["source_offset"].get("txId") == txid
        ]
    )[-1]
    all_events = state["events"]()
    started = next(
        event
        for event in all_events
        if event["event_kind"] == "transaction"
        and event["transaction"]["status"] == "BEGIN"
        and event["source_offset"].get("txId") == txid
    )
    tombstone = next(
        event
        for event in all_events
        if event["event_kind"] == "tombstone"
        and event["key"]["payload"]["id"] == 1
        and event["source_offset"].get("txId") == txid
    )
    indices = [all_events.index(event) for event in (started, deleted, tombstone, ended)]
    assert indices == sorted(indices) and len(set(indices)) == 4
    assert ended["transaction"]["event_count"] == 1
    assert decode_pg_event(deleted).key == {"id": 1}
    decision = next_cdc_window(
        list(enumerate(all_events, 1))[indices[0] :],
        indices[0],
        expected_partition={"server": "ontology_test"},
    )
    assert decision.status == "ready" and decision.unit.kind == "transaction", decision.code
    assert [(change.kind, change.operation) for change in decision.unit.changes] == [
        ("data", "d"),
        ("tombstone", None),
    ]
    (state["directory"] / "delete-events.json").write_text(
        json.dumps(
            {
                "description": "Actual native snapshot, TOAST updates and complete DELETE/tombstone transaction; exclusively synthetic data",
                "runtime": json.loads(
                    (state["directory"] / "result-context.json").read_text(encoding="utf-8")
                ),
                "generation": state["stream"]["generation"],
                "delete_event_indices": indices,
                "events": all_events,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )


@pytest.mark.parametrize("live_cdc", ["empty", "multi", "interrupted", "completion_gap"], indirect=True)
def test_real_snapshot_notifications_prove_empty_scope_and_same_generation_restart(live_cdc):
    state = live_cdc
    initial_generation = state["stream"]["generation"]
    if state["scenario"] == "interrupted":
        assert state["bridge"].rejected.wait(30)
        assert data_events(state, "r")
        assert not snapshot_notifications(state, "COMPLETED")
        state["remove"]()
        execute(state["conn"], "UPDATE items SET name='重扫新值' WHERE id=1")
        execute(state["conn"], "DELETE FROM items WHERE id=2")
        execute(state["conn"], "INSERT INTO items(id,name) VALUES(1001,'中断后新行')")
        state["bridge"].reject_before_commit = False
        with state["app"].state.db.transaction(state["account"]["tenant_id"]) as session:
            session.get(CdcStream, state["stream"]["id"]).lease_until = 0
        state["start"]()  # same generation and exact offset file; native connector restarts its snapshot
    if state["scenario"] == "completion_gap":
        assert state["bridge"].rejected.wait(30)
        assert any(event["snapshot"] == "last" for event in data_events(state, "r"))
        assert not snapshot_notifications(state, "COMPLETED")
        state["remove"]()
        state["bridge"].reject_snapshot_completed = False
        with state["app"].state.db.transaction(state["account"]["tenant_id"]) as session:
            session.get(CdcStream, state["stream"]["id"]).lease_until = 0
        state["start"]()
    completion_type = "SKIPPED" if state["scenario"] == "completion_gap" else "COMPLETED"
    completed = eventually(
        lambda: snapshot_notifications(state, completion_type),
        timeout=60,
        description="native snapshot COMPLETED through durable inbox",
    )
    all_events = state["events"]()
    completed_index = next(
        index for index, event in enumerate(all_events) if event["event_id"] == completed[-1]["event_id"]
    )
    starts = [
        index
        for index, event in enumerate(all_events[:completed_index])
        if event["event_kind"] == "control" and event["value"]["payload"].get("type") == "STARTED"
    ]
    assert starts and (len(starts) >= 2 if state["scenario"] == "interrupted" else len(starts) == 1)
    attempt = all_events[starts[-1] : completed_index + 1]
    rows = [event for event in attempt if event["operation"] == "r"]
    scans = [
        event["value"]["payload"]["additional_data"]
        for event in attempt
        if event["event_kind"] == "control"
        and event["value"]["payload"].get("type") == "TABLE_SCAN_COMPLETED"
    ]
    assert {scan["scanned_collection"] for scan in scans} == state["expected_resources"]
    for scan in scans:
        assert scan["status"] == "SUCCEEDED"
        assert set(scan["data_collections"].split(",")) == state["expected_resources"]
        assert int(scan["total_rows_scanned"]) == sum(
            event["value"]["payload"]["source"]["schema"] + "." + event["value"]["payload"]["source"]["table"]
            == scan["scanned_collection"]
            for event in rows
        )
    assert completed_index > max((all_events.index(event) for event in rows), default=starts[-1])
    assert isinstance(completed[-1]["source_offset"]["lsn"], int)
    assert "snapshot" not in completed[-1]["source_offset"]
    assert len(rows) == {"empty": 0, "multi": 2, "interrupted": 24, "completion_gap": 2}[state["scenario"]]
    if state["scenario"] == "interrupted":
        assert (
            next(event for event in rows if event["key"]["payload"]["id"] == 1)["value"]["payload"]["after"][
                "name"
            ]
            == "重扫新值"
        )
        assert 2 not in {event["key"]["payload"]["id"] for event in rows}
        assert 1001 in {event["key"]["payload"]["id"] for event in rows}
    with state["app"].state.db.transaction(state["account"]["tenant_id"]) as session:
        stream = session.get(CdcStream, state["stream"]["id"])
        assert stream.generation == initial_generation
        assert stream.projected_sequence == stream.indexed_sequence == 0
    decision = next_cdc_window(
        list(enumerate(all_events, 1)),
        0,
        expected_partition={"server": "ontology_test"},
        expected_snapshot_resources=sorted(state["expected_resources"]),
    )
    assert decision.status == "ready", decision.code
    assert decision.unit.kind == "snapshot" and len(decision.unit.changes) == len(rows)
    assert decision.unit.snapshot_proof.kind == (
        "resumed_completed_offset" if state["scenario"] == "completion_gap" else "completed_notification"
    )
    if state["scenario"] == "interrupted":
        assert decision.unit.snapshot_proof.superseded_ranges
        assert len(decision.unit.records) > len(attempt)
    (state["directory"] / "snapshot-events.json").write_text(
        json.dumps(
            {
                "description": "Actual native PostgreSQL snapshot notifications; exclusively self-owned synthetic data",
                "scenario": state["scenario"],
                "generation": initial_generation,
                "events": all_events,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
