"""Select one complete, bounded projection unit from an already durable inbox.

There are no writes here. Waiting/blocked/budget results deliberately expose no
end cursor to commit. The caller owns stream/generation, ACL, mapping validation
and an atomic transaction for business projection plus cursor advancement.
"""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Literal

from ontology.cdc.decode import (
    CdcDecodeError,
    DecodedChange,
    decode_pg_event,
    fail,
    integer,
    validate_transaction_window,
)


@dataclass(frozen=True)
class SnapshotProof:
    kind: Literal["completed_notification", "resumed_completed_offset"]
    started_sequence: int
    completed_sequence: int
    native_start_id: str
    native_completion_id: str
    resources: tuple[str, ...]
    table_counts: dict[str, int]
    source_position: dict[str, Any]
    superseded_ranges: tuple[tuple[int, int], ...]


@dataclass(frozen=True)
class ProjectionWindow:
    kind: Literal["snapshot", "transaction", "noop"]
    start_sequence: int
    end_sequence: int
    changes: tuple[DecodedChange, ...]
    records: tuple[tuple[int, dict[str, Any]], ...]
    schema_fingerprints: dict[str, str]
    snapshot_proof: SnapshotProof | None = None


@dataclass(frozen=True)
class WindowDecision:
    status: Literal["ready", "waiting", "blocked", "budget_exceeded"]
    code: str
    unit: ProjectionWindow | None = None


def schema_fingerprint(raw):
    schema = raw["value"]["schema"]
    fields = {item["field"]: item for item in schema.get("fields", [])}
    # Capture fields used for mapping, not changing per-record source metadata.
    relevant = {"key": raw["key"]["schema"], "before": fields.get("before"), "after": fields.get("after")}
    material = json.dumps(
        relevant, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


class SnapshotAttempt:
    """One native STARTED..completion attempt, with retained superseded prefixes."""

    def __init__(self, expected_resources):
        if not expected_resources or isinstance(expected_resources, (str, bytes)):
            fail(
                "snapshot_expected_scope_required",
                "Baseline proof needs an explicit reviewed collection scope",
            )
        resources = tuple(sorted(expected_resources))
        if len(resources) != len(set(resources)) or any(
            not isinstance(resource, str)
            or "," in resource
            or len(resource.split(".")) != 2
            or not all(resource.split("."))
            for resource in resources
        ):
            fail(
                "unsupported_snapshot_scope",
                "The notification identifier encoding cannot unambiguously represent this scope",
            )
        self.resources = resources
        self.started = None
        self.superseded = []
        self.changes = []
        self.fingerprints = {}

    def accept(self, sequence, raw, change):
        notice = change.notification
        if notice and notice.status == "STARTED":
            if self.started is not None and notice.connector_name != self.connector_name:
                fail(
                    "snapshot_connector_changed", "A resumed attempt must use the same native connector scope"
                )
            if self.started is not None:
                self.superseded.append((self.started, sequence - 1))
            self.started, self.start_id = sequence, notice.native_id
            self.connector_name = notice.connector_name
            self.changes, self.fingerprints = [], {}
            self.keys, self.in_progress = set(), set()
            self.scanned, self.counts = {}, Counter()
            self.position = self.row_timestamp = self.last_position = None
            self.aborted = False
            return None
        if notice and notice.connector_name != self.connector_name:
            fail("snapshot_connector_changed", "Snapshot control records must belong to one native connector")
        if notice and notice.status == "ABORTED":
            self.aborted = True
            return None
        if self.aborted:
            fail(
                "snapshot_aborted_requires_restart",
                "An aborted native attempt requires a new STARTED boundary",
            )
        if notice and notice.status in {"IN_PROGRESS", "TABLE_SCAN_COMPLETED"}:
            if notice.resources != self.resources:
                fail(
                    "snapshot_scope_mismatch",
                    "Actual capture scope differs from the reviewed expected collections",
                )
            self.check_position(change, completing=False)
            if notice.status == "IN_PROGRESS":
                if notice.collection in self.in_progress:
                    fail(
                        "snapshot_duplicate_table_start",
                        "A table was started more than once inside one snapshot attempt",
                    )
                self.in_progress.add(notice.collection)
            else:
                if notice.collection not in self.in_progress or notice.collection in self.scanned:
                    fail(
                        "snapshot_table_boundary_invalid",
                        "Table completion needs exactly one matching progress boundary",
                    )
                self.scanned[notice.collection] = notice.row_count
            return None
        if notice and notice.status in {"COMPLETED", "SKIPPED"}:
            self.check_position(change, completing=True)
            if set(self.scanned) != set(self.resources) or self.scanned != {
                key: self.counts[key] for key in self.resources
            }:
                fail(
                    "snapshot_table_count_mismatch",
                    "Every expected table needs successful scan counts matching all received rows",
                )
            if self.changes and self.last_position is None:
                fail(
                    "snapshot_end_not_proven",
                    "Non-empty snapshot lacks its native final row and completion flags",
                )
            if self.row_timestamp is not None and change.source_position.get("ts_usec") != self.row_timestamp:
                fail("snapshot_boundary_changed", "Completion offset differs from the source snapshot time")
            if notice.status == "SKIPPED" and (
                self.last_position is None
                or any(
                    change.source_position.get(key) != self.last_position[key]
                    for key in ("lsn", "txId", "ts_usec")
                )
            ):
                fail(
                    "snapshot_skip_not_proven",
                    "SKIPPED requires the exact previously received completed last-row offset",
                )
            return SnapshotProof(
                "completed_notification" if notice.status == "COMPLETED" else "resumed_completed_offset",
                self.started,
                sequence,
                self.start_id,
                notice.native_id,
                self.resources,
                dict(self.scanned),
                dict(change.source_position),
                tuple(self.superseded),
            )
        if notice or change.kind != "data" or change.operation != "r":
            fail(
                "snapshot_interrupted_before_completion",
                "Only native snapshot rows and notifications may complete this attempt",
            )
        if change.resource_id not in self.resources or change.resource_id not in self.in_progress:
            fail("snapshot_scope_mismatch", "Snapshot row is outside its declared capture scope")
        self.check_position(change, completing=False)
        row_time = integer(change.source_position.get("ts_usec"))
        if self.row_timestamp is None:
            self.row_timestamp = row_time
        if self.row_timestamp != row_time or self.last_position is not None:
            fail(
                "snapshot_boundary_changed",
                "Snapshot rows cannot change source time or follow the completed last row",
            )
        if change.snapshot not in {
            "first",
            "true",
            "last",
            "first_in_data_collection",
            "last_in_data_collection",
            True,
        }:
            fail("unsupported_snapshot_mode", "Only native initial snapshot row markers are supported")
        identity = (
            change.resource_id,
            json.dumps(change.identity_key, sort_keys=True, separators=(",", ":")),
        )
        if identity in self.keys:
            fail("duplicate_snapshot_identity", "Snapshot attempt contains the same source identity twice")
        self.keys.add(identity)
        fingerprint = schema_fingerprint(raw)
        if self.fingerprints.setdefault(change.resource_id, fingerprint) != fingerprint:
            fail("schema_changed_inside_window", "Snapshot schema changed inside its current attempt")
        self.counts[change.resource_id] += 1
        self.changes.append(change)
        if change.snapshot == "last":
            if (
                raw["source_offset"].get("snapshot_completed") is not True
                or raw["source_offset"].get("last_snapshot_record") is not True
            ):
                fail("snapshot_end_not_proven", "Final row lacks native snapshot completion flags")
            self.last_position = dict(change.source_position)
        return None

    def check_position(self, change, *, completing):
        position = (
            integer(change.source_position.get("lsn"), minimum=0),
            integer(change.source_position.get("txId"), minimum=0),
        )
        integer(change.source_position.get("ts_usec"))
        if self.position is None:
            self.position = position
        if position != self.position:
            fail("snapshot_boundary_changed", "Snapshot attempt changed its native LSN or transaction")
        if completing:
            if "snapshot" in change.source_position:
                fail(
                    "snapshot_end_not_proven",
                    "Completion notification still carries a running-snapshot offset",
                )
        elif change.source_position.get("snapshot") != "INITIAL":
            fail("unsupported_snapshot_mode", "Only initial snapshot offsets are accepted for this baseline")


def next_cdc_window(
    records: Sequence[tuple[int, dict[str, Any]]],
    after_sequence: int,
    *,
    expected_partition: dict[str, Any] | None = None,
    max_events: int = 10_000,
    max_bytes: int = 64 * 1024 * 1024,
    allow_snapshot: bool = True,
    expected_snapshot_resources: Sequence[str] | None = None,
    unavailable_value_placeholder: str | None = "__debezium_unavailable_value",
) -> WindowDecision:
    if type(after_sequence) is not int or after_sequence < 0 or max_events < 1 or max_bytes < 1:
        return WindowDecision("blocked", "invalid_window_configuration")
    if not records:
        return WindowDecision("waiting", "inbox_empty")
    partition = expected_partition
    collected, changes = [], []
    fingerprints = {}
    byte_count = 0
    kind, begin = None, None
    snapshot_attempt = None
    snapshot_proof = None

    def ready():
        return WindowDecision(
            "ready",
            "complete",
            ProjectionWindow(
                kind,
                collected[0][0],
                collected[-1][0],
                tuple(changes),
                tuple(collected),
                dict(fingerprints),
                snapshot_proof,
            ),
        )

    try:
        for index, entry in enumerate(records):
            if index >= max_events:
                return WindowDecision("budget_exceeded", "window_event_budget")
            sequence, raw = entry
            if type(sequence) is not int or sequence != after_sequence + index + 1:
                return WindowDecision("blocked", "inbox_sequence_gap_or_reorder")
            if not isinstance(raw, dict):
                return WindowDecision("blocked", "invalid_envelope")
            byte_count += len(
                json.dumps(raw, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")
            )
            if byte_count > max_bytes:
                return WindowDecision("budget_exceeded", "window_byte_budget")
            change = decode_pg_event(raw, unavailable_value_placeholder=unavailable_value_placeholder)
            if partition is None:
                partition = change.source_partition
            if change.source_partition != partition:
                return WindowDecision("blocked", "source_partition_changed")
            if change.kind in {"schema", "control"} and change.notification is None:
                return WindowDecision("blocked", "source_control_requires_review")
            if change.operation == "t":
                return WindowDecision("blocked", "truncate_requires_review")
            if change.kind == "data" and kind != "snapshot":
                fingerprint = schema_fingerprint(raw)
                previous = fingerprints.setdefault(change.resource_id, fingerprint)
                if fingerprint != previous:
                    return WindowDecision("blocked", "schema_changed_inside_window")
            collected.append((sequence, raw))
            if snapshot_attempt is not None:
                snapshot_proof = snapshot_attempt.accept(sequence, raw, change)
                changes, fingerprints = snapshot_attempt.changes, snapshot_attempt.fingerprints
                if snapshot_proof is not None:
                    return ready()
                continue
            if kind is None:
                if change.notification:
                    if change.notification.status == "SKIPPED" and not allow_snapshot:
                        kind = "noop"
                        return (
                            ready()
                        )  # Caller already has its persisted baseline; SKIPPED adds no new proof.
                    if change.notification.status != "STARTED":
                        return WindowDecision("blocked", "snapshot_start_notification_required")
                    if not allow_snapshot:
                        return WindowDecision("blocked", "unexpected_snapshot_requires_rebaseline")
                    kind = "snapshot"
                    snapshot_attempt = SnapshotAttempt(expected_snapshot_resources)
                    snapshot_attempt.accept(sequence, raw, change)
                    continue
                if change.kind == "heartbeat":
                    kind = "noop"
                    return ready()
                if change.kind == "data" and change.operation == "r":
                    if not allow_snapshot:
                        return WindowDecision("blocked", "unexpected_snapshot_requires_rebaseline")
                    return WindowDecision("blocked", "snapshot_start_notification_required")
                elif change.boundary and change.boundary.phase == "begin":
                    kind, begin = "transaction", change.boundary
                    continue
                else:
                    return WindowDecision("blocked", "missing_snapshot_or_transaction_start")
            if kind == "transaction":
                if change.boundary:
                    if change.boundary.phase != "end":
                        return WindowDecision("blocked", "nested_transaction_begin")
                    validate_transaction_window(begin, changes, change.boundary)
                    return ready()
                if change.kind not in {"data", "tombstone"} or change.operation == "r":
                    return WindowDecision("blocked", "unexpected_event_inside_transaction")
                changes.append(change)
        if len(collected) >= max_events:
            return WindowDecision("budget_exceeded", "window_event_budget")
        return WindowDecision(
            "waiting", "snapshot_end_pending" if kind == "snapshot" else "transaction_commit_pending"
        )
    except CdcDecodeError as exc:
        return WindowDecision("blocked", exc.code)
    except (KeyError, TypeError, ValueError, OverflowError, RecursionError):
        return WindowDecision("blocked", "invalid_envelope")
