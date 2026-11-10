import copy
import json

import pytest
from ontology.cdc.windows import next_cdc_window

from tests.support.paths import REPO_ROOT


@pytest.fixture
def native_events():
    path = REPO_ROOT / "integrations/debezium/protocol/pg-events.json"
    return json.loads(path.read_text(encoding="utf-8"))["events"]


@pytest.fixture
def native_snapshots():
    path = REPO_ROOT / "integrations/debezium/protocol/pg-snapshot-events.json"
    return {
        key: value["events"]
        for key, value in json.loads(path.read_text(encoding="utf-8"))["scenarios"].items()
    }


SCOPE = ["public.items", "public.zz_empty"]


def snapshot_window(events, **kwargs):
    return next_cdc_window(numbered(events), 0, expected_snapshot_resources=SCOPE, **kwargs)


def transaction(events):
    begin = next(
        event
        for event in events
        if event["event_kind"] == "transaction" and event["transaction"]["status"] == "BEGIN"
    )
    return [event for event in events if event["source_offset"]["txId"] == begin["source_offset"]["txId"]]


def numbered(events, after=0):
    return list(enumerate(events, start=after + 1))


def assert_no_advance(decision, status):
    assert decision.status == status
    assert decision.unit is None


def test_actual_snapshot_then_transaction_produce_separate_complete_units(native_events, native_snapshots):
    snapshots = native_snapshots["multi"]
    tx = transaction(native_events)
    first = snapshot_window([*snapshots, *tx])
    assert first.status == "ready"
    assert first.unit.kind == "snapshot"
    assert first.unit.end_sequence == len(snapshots) == 8
    assert len(first.unit.changes) == 2
    second = next_cdc_window(numbered(tx, after=8), 8, allow_snapshot=False)
    assert second.status == "ready"
    assert second.unit.kind == "transaction"
    assert len(second.unit.changes) == 3
    assert second.unit.end_sequence == 8 + len(tx)
    assert set(first.unit.schema_fingerprints) == {"public.items"}


def test_incomplete_snapshot_and_transaction_keep_cursor(native_events, native_snapshots):
    assert_no_advance(next_cdc_window([], 0), "waiting")
    assert_no_advance(snapshot_window(native_snapshots["multi"][:-1]), "waiting")
    tx = transaction(native_events)
    result = next_cdc_window(numbered(tx[:-1], 10), 10)
    assert_no_advance(result, "waiting")
    assert result.code == "transaction_commit_pending"
    orphan = next_cdc_window(numbered(tx[1:], 10), 10)
    assert_no_advance(orphan, "blocked")
    assert orphan.code == "missing_snapshot_or_transaction_start"


def test_budgets_are_explicit_and_do_not_skip_a_large_unit(native_events):
    tx = transaction(native_events)
    assert_no_advance(next_cdc_window(numbered(tx), 0, max_events=2), "budget_exceeded")
    assert_no_advance(next_cdc_window(numbered(tx), 0, max_bytes=100), "budget_exceeded")
    assert next_cdc_window(numbered(tx), 0, max_events=len(tx)).status == "ready"


def test_missing_sequence_wrong_partition_and_nested_begin_block(native_events):
    tx = transaction(native_events)
    entries = numbered(tx)
    entries[1] = (20, entries[1][1])
    assert_no_advance(next_cdc_window(entries, 0), "blocked")
    wrong = copy.deepcopy(tx)
    wrong[1]["source_partition"] = {"server": "foreign"}
    assert next_cdc_window(numbered(wrong), 0).code == "source_partition_changed"
    assert (
        next_cdc_window(numbered(tx), 0, expected_partition={"server": "foreign"}).code
        == "source_partition_changed"
    )
    assert next_cdc_window(numbered([tx[0], tx[0]]), 0).code == "nested_transaction_begin"


def test_control_schema_and_truncate_require_governance(native_events):
    for kind in ("schema", "control"):
        event = copy.deepcopy(native_events[0])
        event["event_kind"] = kind
        result = next_cdc_window(numbered([event]), 0)
        assert_no_advance(result, "blocked")
        assert result.code == "source_control_requires_review"
    truncate = next(event for event in native_events if event["operation"] == "t")
    assert next_cdc_window(numbered([truncate]), 0).code == "truncate_requires_review"
    heartbeat = copy.deepcopy(native_events[0])
    heartbeat["event_kind"], heartbeat["operation"] = "heartbeat", None
    result = next_cdc_window(numbered([heartbeat]), 0)
    assert result.status == "ready" and result.unit.kind == "noop"


def test_ordinal_count_and_schema_drift_fail_closed(native_events):
    tx = transaction(native_events)
    wrong = copy.deepcopy(tx)
    wrong[2]["transaction"]["total_order"] = wrong[2]["value"]["payload"]["transaction"]["total_order"] = 1
    assert next_cdc_window(numbered(wrong), 0).code == "transaction_order"
    wrong = copy.deepcopy(tx)
    wrong[-1]["transaction"]["event_count"] += 1
    wrong[-1]["value"]["payload"]["event_count"] += 1
    assert next_cdc_window(numbered(wrong), 0).code == "invalid_transaction"
    wrong = copy.deepcopy(tx)
    after_schema = next(field for field in wrong[2]["value"]["schema"]["fields"] if field["field"] == "after")
    after_schema["fields"].append({"field": "added", "type": "string", "optional": True})
    wrong[2]["value"]["payload"]["after"]["added"] = "new schema"
    assert next_cdc_window(numbered(wrong), 0).code == "schema_changed_inside_window"


def test_snapshot_duplicate_keys_and_unproven_end_block(native_events, native_snapshots):
    snapshots = copy.deepcopy(native_snapshots["multi"])
    assert snapshot_window(snapshots, allow_snapshot=False).code == "unexpected_snapshot_requires_rebaseline"
    snapshots[-2]["source_offset"]["snapshot_completed"] = False
    assert snapshot_window(snapshots).code == "snapshot_end_not_proven"
    snapshots = copy.deepcopy(native_snapshots["multi"])
    snapshots[-2]["key"]["payload"] = copy.deepcopy(snapshots[2]["key"]["payload"])
    snapshots[-2]["value"]["payload"]["after"]["id"] = 1
    assert snapshot_window(snapshots).code == "duplicate_snapshot_identity"
    assert (
        next_cdc_window(numbered([native_events[0], native_events[1]]), 0).code
        == "snapshot_start_notification_required"
    )


def test_tombstone_does_not_inflate_transaction_count(native_events):
    tx = copy.deepcopy(transaction(native_events))
    tombstone = copy.deepcopy(next(event for event in native_events if event["event_kind"] == "tombstone"))
    tombstone["source_offset"] = copy.deepcopy(tx[-2]["source_offset"])
    tx.insert(-1, tombstone)
    result = next_cdc_window(numbered(tx), 0)
    assert result.status == "ready"
    assert len(result.unit.changes) == 4  # 3 source rows + 1 transport deletion marker


@pytest.mark.parametrize(
    "scenario,count,proof_kind",
    [
        ("empty", 0, "completed_notification"),
        ("multi", 2, "completed_notification"),
        ("interrupted", 24, "completed_notification"),
        ("completion_gap", 2, "resumed_completed_offset"),
    ],
)
def test_actual_native_notification_baselines_and_superseded_attempts(
    native_snapshots, scenario, count, proof_kind
):
    events = native_snapshots[scenario]
    expected = ["public.items"] if scenario == "empty" else SCOPE
    result = next_cdc_window(numbered(events), 0, expected_snapshot_resources=expected)
    assert result.status == "ready", result.code
    unit = result.unit
    assert unit.kind == "snapshot" and len(unit.changes) == count
    assert unit.end_sequence == len(events)
    assert unit.snapshot_proof.kind == proof_kind
    assert unit.snapshot_proof.resources == tuple(expected)
    assert unit.snapshot_proof.table_counts == {
        resource: count if resource == "public.items" else 0 for resource in expected
    }
    assert len(unit.records) == len(
        events
    )  # Includes all native controls and old prefixes, never silently drops evidence.
    if scenario == "interrupted":
        assert unit.snapshot_proof.superseded_ranges == ((1, 3),)
        assert unit.snapshot_proof.started_sequence == 4
        assert {change.key["id"] for change in unit.changes} == {*range(3, 25), 1, 1001}
        assert (
            next(change for change in unit.changes if change.key["id"] == 1).after.complete_values()["name"]
            == "重扫新值"
        )
        assert_no_advance(snapshot_window(events[:5]), "waiting")
    else:
        assert unit.snapshot_proof.superseded_ranges == ()


def test_snapshot_scope_counts_native_completion_and_skip_are_not_guessed(native_snapshots):
    events = native_snapshots["multi"]
    assert next_cdc_window(numbered(events), 0).code == "snapshot_expected_scope_required"
    assert (
        next_cdc_window(numbered(events), 0, expected_snapshot_resources=["public.items"]).code
        == "snapshot_scope_mismatch"
    )
    wrong = copy.deepcopy(events)
    scan = next(
        event
        for event in wrong
        if event["event_kind"] == "control" and event["value"]["payload"]["type"] == "TABLE_SCAN_COMPLETED"
    )
    scan["value"]["payload"]["additional_data"]["total_rows_scanned"] = "1"
    assert snapshot_window(wrong).code == "snapshot_table_count_mismatch"
    without_empty_scan = [
        event
        for event in events
        if not (
            event["event_kind"] == "control"
            and event["value"]["payload"]["additional_data"].get("scanned_collection") == "public.zz_empty"
        )
    ]
    assert snapshot_window(without_empty_scan).code == "snapshot_table_count_mismatch"
    skipped = native_snapshots["completion_gap"][-1]
    assert snapshot_window([skipped]).code == "snapshot_start_notification_required"
    prior_baseline = snapshot_window([skipped], allow_snapshot=False)
    assert (
        prior_baseline.status == "ready"
        and prior_baseline.unit.kind == "noop"
        and prior_baseline.unit.snapshot_proof is None
    )
    wrong = copy.deepcopy(native_snapshots["completion_gap"])
    wrong[-1]["source_offset"]["lsn"] += 1
    assert snapshot_window(wrong).code == "snapshot_boundary_changed"
    empty_skip = copy.deepcopy(native_snapshots["empty"])
    empty_skip[-1]["value"]["payload"]["type"] = "SKIPPED"
    assert (
        next_cdc_window(numbered(empty_skip), 0, expected_snapshot_resources=["public.items"]).code
        == "snapshot_skip_not_proven"
    )


def test_control_schema_and_native_key_mismatches_cannot_claim_completion(native_snapshots):
    events = copy.deepcopy(native_snapshots["multi"])
    events[-1]["key"]["payload"]["id"] = "00000000-0000-0000-0000-000000000000"
    assert snapshot_window(events).code == "unsupported_snapshot_notification"
    events = copy.deepcopy(native_snapshots["multi"])
    events[-1]["value"]["schema"]["name"] = "untrusted.Notification"
    assert snapshot_window(events).code == "source_control_requires_review"
    events = copy.deepcopy(native_snapshots["multi"])
    events[3]["value"]["payload"]["additional_data"]["status"] = "SQL_EXCEPTION"
    assert snapshot_window(events).code == "snapshot_scan_failed"
    assert_no_advance(snapshot_window(native_snapshots["interrupted"], max_events=10), "budget_exceeded")


@pytest.mark.parametrize("boundary", ["first_progress", "empty_table_scan", "completion"])
def test_same_scope_different_native_snapshot_position_is_rejected(native_snapshots, boundary):
    events = copy.deepcopy(native_snapshots["multi"])
    index = {"first_progress": 1, "empty_table_scan": 5, "completion": 7}[boundary]
    events[index]["source_offset"]["lsn"] += 1
    assert snapshot_window(events).code == "snapshot_boundary_changed"


def test_same_scope_missing_nonempty_table_evidence_cannot_complete(native_snapshots):
    events = native_snapshots["multi"]
    without_scan = [
        event
        for event in events
        if not (
            event["event_kind"] == "control"
            and event["value"]["payload"]["additional_data"].get("scanned_collection") == "public.items"
        )
    ]
    assert snapshot_window(without_scan).code == "snapshot_table_count_mismatch"
