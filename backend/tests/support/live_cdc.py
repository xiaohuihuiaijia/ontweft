"""Real PostgreSQL WAL -> Debezium -> real Python inbox; explicit Docker opt-in.

ONTOLOGY_LIVE_CDC=1 pytest backend/tests/live/test_live_cdc.py -q --tb=short
Build integrations/debezium first. Only uniquely labelled test containers are removed.
The receiver uses the real application with a disposable SQLite inbox in this test;
PostgreSQL inbox locking/RLS is covered separately by test_postgres.py.
"""

from __future__ import annotations

import hashlib
import json
import secrets
import socket
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from fastapi.testclient import TestClient
from ontology.api import create_app
from ontology.cdc.models import CdcEvent, CdcStream
from ontology.cli import bootstrap
from ontology.core.config import Settings
from sqlalchemy import select

from tests.support.live_connectors import IMAGES, admin_connection, docker, execute
from tests.support.paths import REPO_ROOT

ADAPTER_IMAGE = "ontology-debezium-adapter:3.3.2-snapshot-notifications"


def eventually(check, *, timeout=45, description="CDC condition"):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = check()
        if result:
            return result
        time.sleep(0.2)
    raise AssertionError(f"Timed out waiting for {description}")


class ReceiverBridge:
    """Only injects transport faults around the real API; never simulates WAL events."""

    def __init__(self, client):
        self.client = client
        self.calls = []
        self.lock = threading.Lock()
        self.drop_after_commit = False
        self.committed_without_ack = threading.Event()
        self.release_response = threading.Event()
        self.reject_before_commit = False
        self.pause_after_next_ack = False
        self.pause_after_snapshot_ack = False
        self.reject_snapshot_completed = False
        self.rejected_calls = []
        self.rejected = threading.Event()
        bridge = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass  # Never log source payloads, credentials or bearer headers.

            def do_POST(self):
                data = self.rfile.read(int(self.headers.get("Content-Length", "0")))
                reject_completed = (
                    bridge.reject_snapshot_completed
                    and self.path.endswith("/append")
                    and any(
                        item.get("event_kind") == "control"
                        and item["value"]["payload"].get("type") == "COMPLETED"
                        for item in json.loads(data)["events"]
                    )
                )
                if self.path.endswith("/append") and (bridge.reject_before_commit or reject_completed):
                    with bridge.lock:
                        bridge.rejected_calls.append(json.loads(data))
                    bridge.rejected.set()
                    self.send_response(503)
                    self.end_headers()
                    return
                response = bridge.client.post(
                    self.path,
                    content=data,
                    headers={
                        "authorization": self.headers.get("Authorization", ""),
                        "content-type": "application/json",
                    },
                )
                if self.path.endswith("/append"):
                    with bridge.lock:
                        bridge.calls.append(
                            {
                                "batch": json.loads(data),
                                "status": response.status_code,
                                "response": response.json(),
                            }
                        )
                    if response.status_code == 200 and bridge.pause_after_next_ack:
                        bridge.pause_after_next_ack = False
                        bridge.reject_before_commit = True
                    if (
                        response.status_code == 200
                        and bridge.pause_after_snapshot_ack
                        and any(item.get("operation") == "r" for item in json.loads(data)["events"])
                    ):
                        bridge.pause_after_snapshot_ack = False
                        bridge.reject_before_commit = True
                    if response.status_code == 200 and bridge.drop_after_commit:
                        bridge.drop_after_commit = False
                        bridge.committed_without_ack.set()
                        bridge.release_response.wait(20)
                        try:
                            self.connection.shutdown(socket.SHUT_RDWR)
                        except OSError:
                            pass
                        return
                try:
                    self.send_response(response.status_code)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(response.content)))
                    self.end_headers()
                    self.wfile.write(response.content)
                except (BrokenPipeError, ConnectionResetError):
                    pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def close(self):
        self.release_response.set()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(5)


@pytest.fixture
def live_cdc(request):
    scenario = getattr(request, "param", "normal")
    run_id = uuid.uuid4().hex[:12]
    label = "ontology.cdc-test-run"
    prefix = f"ontology-source-test-cdc-{run_id}"
    pg_name, adapter_name = prefix + "-pg", prefix + "-adapter"
    root = REPO_ROOT
    directory = root / "var" / "live-cdc" / run_id
    directory.mkdir(parents=True)
    (directory / "offsets").mkdir()
    admin_password, replication_password = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
    envfile = directory / "postgres.env"
    envfile.write_text(f"POSTGRES_PASSWORD={admin_password}\nPOSTGRES_DB=ontology_test\n", encoding="utf-8")
    containers = []
    conn, bridge, client = None, None, None
    docker("image", "inspect", ADAPTER_IMAGE, "--format", "{{.Id}}")
    try:
        docker(
            "run",
            "--detach",
            "--name",
            pg_name,
            "--label",
            f"{label}={run_id}",
            "--memory",
            "512m",
            "--cpus",
            "1",
            "--env-file",
            str(envfile),
            "--publish",
            "127.0.0.1::5432",
            IMAGES["postgres"],
            "-c",
            "wal_level=logical",
            "-c",
            "max_replication_slots=4",
            "-c",
            "max_wal_senders=4",
        )
        containers.append(pg_name)
        binding = docker("port", pg_name, "5432/tcp")
        assert binding.startswith("127.0.0.1:")
        port = int(binding.rsplit(":", 1)[1])
        for _ in range(60):
            try:
                conn = admin_connection("postgres", port, admin_password)
                break
            except Exception:  # noqa: BLE001 -- credentials/driver errors are not diagnostics.
                time.sleep(0.5)
        if conn is None:
            raise RuntimeError("Disposable PostgreSQL failed to start")
        execute(
            conn,
            "CREATE TABLE items (id BIGINT PRIMARY KEY, name TEXT NOT NULL, amount NUMERIC(30,18), raw BYTEA, optional TEXT)",
        )
        execute(conn, "ALTER TABLE items REPLICA IDENTITY FULL")
        if scenario != "empty":
            execute(
                conn,
                "INSERT INTO items VALUES (1,'初始',123456789012.123456789012345678,decode('00ff','hex'),NULL),(2,'保留',NULL,NULL,'x')",
            )
        tables = ["items"]
        if scenario in {"multi", "interrupted", "completion_gap"}:
            execute(conn, "CREATE TABLE zz_empty (id BIGINT PRIMARY KEY, name TEXT)")
            tables.append("zz_empty")
        if scenario == "interrupted":
            execute(conn, "INSERT INTO items(id,name) SELECT i,'中断样本' FROM generate_series(3,24) AS i")
        execute(conn, f"CREATE ROLE ontology_cdc LOGIN REPLICATION PASSWORD '{replication_password}'")
        execute(conn, "GRANT CONNECT ON DATABASE ontology_test TO ontology_cdc")
        execute(conn, "GRANT USAGE ON SCHEMA public TO ontology_cdc")
        execute(conn, "GRANT SELECT ON TABLE " + ",".join(tables) + " TO ontology_cdc")
        execute(conn, "CREATE PUBLICATION ontology_test_pub FOR TABLE " + ",".join(tables))
        settings = Settings(
            database_url=f"sqlite:///{directory / 'inbox.db'}",
            data_dir=directory / "data",
            allowed_source_hosts=["127.0.0.1"],
            allow_private_source_addresses=True,
        )
        app = create_app(settings)
        account_password = secrets.token_urlsafe(32)
        account = bootstrap(
            app.state.db,
            tenant_slug="cdc",
            tenant_name="CDC test",
            email="cdc@example.test",
            password=account_password,
        )
        client = TestClient(app)
        client.__enter__()
        login = client.post(
            "/api/auth/login",
            json={"tenant_slug": "cdc", "email": "cdc@example.test", "password": account_password},
        )
        assert login.status_code == 200
        source_response = client.post(
            "/api/sources",
            json={
                "project_id": account["project_id"],
                "name": "Disposable WAL source",
                "kind": "postgres",
                "config": {
                    "host": "127.0.0.1",
                    "port": port,
                    "database": "ontology_test",
                    "username": "ontology_cdc",
                },
                "credentials": {"password": replication_password},
            },
        )
        assert source_response.status_code == 201
        source = source_response.json()
        stream_response = client.post(
            "/api/cdc/streams",
            json={"source_id": source["id"], "expected_source_revision": source["revision"]},
        )
        assert stream_response.status_code == 201, stream_response.text
        stream, token = stream_response.json()["stream"], stream_response.json()["token"]
        (directory / "token").write_text(token, encoding="utf-8")
        bridge = ReceiverBridge(client)
        bridge.pause_after_snapshot_ack = scenario == "interrupted"
        bridge.reject_snapshot_completed = scenario == "completion_gap"
        properties = {
            "inbox.stream.url": f"http://host.docker.internal:{bridge.server.server_port}/api/cdc/streams/{stream['id']}",
            "inbox.token.file": "/state/token",
            "inbox.generation": stream["generation"],
            "inbox.allowed.hosts": "host.docker.internal",
            "inbox.allow.http": "true",
            "inbox.max.events": "1",
            "name": "ontology-test",
            "database.hostname": "host.docker.internal",
            "database.port": str(port),
            "source.allowed.hosts": "host.docker.internal",
            "database.user": "ontology_cdc",
            "database.password": replication_password,
            "database.dbname": "ontology_test",
            "topic.prefix": "ontology_test",
            "table.include.list": ",".join("public." + table for table in tables),
            "slot.name": "ontology_test_slot",
            "publication.name": "ontology_test_pub",
            "offset.storage.file.filename": "/state/offsets/offsets.dat",
            "poll.interval.ms": "100",
            "max.batch.size": "1" if scenario == "completion_gap" else "2",
            "max.queue.size": "8",
            "snapshot.fetch.size": "1",
            "status.update.interval.ms": "1000",
        }
        (directory / "adapter.properties").write_text(
            "\n".join(f"{key}={value}" for key, value in properties.items()) + "\n", encoding="utf-8"
        )

        def start_adapter():
            docker(
                "run",
                "--detach",
                "--name",
                adapter_name,
                "--label",
                f"{label}={run_id}",
                "--memory",
                "640m",
                "--cpus",
                "1",
                "--mount",
                f"type=bind,source={directory},target=/state",
                ADAPTER_IMAGE,
                "/state/adapter.properties",
            )
            if adapter_name not in containers:
                containers.append(adapter_name)

        def remove_adapter():
            if (
                docker(
                    "inspect", "--format", '{{index .Config.Labels "ontology.cdc-test-run"}}', adapter_name
                )
                != run_id
            ):
                raise RuntimeError("Refusing to remove a container without matching test ownership")
            docker("rm", "--force", adapter_name)
            containers.remove(adapter_name)

        def events():
            with app.state.db.transaction(account["tenant_id"]) as session:
                return [
                    row.record
                    for row in session.scalars(
                        select(CdcEvent).where(CdcEvent.stream_id == stream["id"]).order_by(CdcEvent.sequence)
                    )
                ]

        start_adapter()
        (directory / "result-context.json").write_text(
            json.dumps(
                {
                    "postgres_image": IMAGES["postgres"],
                    "postgres_version": execute(conn, "SELECT version()")[0][0],
                    "adapter_image_id": docker("image", "inspect", ADAPTER_IMAGE, "--format", "{{.Id}}"),
                    "debezium_version": "3.3.2.Final",
                    "debezium_commit": "a9398538861cc4986906c424700edb2300447cc8",
                    "receiver": "real Python API, disposable SQLite inbox",
                    "source_port_binding": "127.0.0.1",
                },
                indent=2,
            ),
            encoding="utf-8",
        )
        yield {
            "conn": conn,
            "app": app,
            "client": client,
            "stream": stream,
            "account": account,
            "directory": directory,
            "bridge": bridge,
            "events": events,
            "start": start_adapter,
            "remove": remove_adapter,
            "adapter_name": adapter_name,
            "scenario": scenario,
            "expected_resources": {"public." + table for table in tables},
        }
    finally:
        if bridge:
            bridge.close()
        if client:
            client.__exit__(None, None, None)
        if conn:
            conn.close()
        for name in reversed(containers):
            if not name.startswith("ontology-source-test-cdc-"):
                raise RuntimeError("Test cleanup target outside allowed prefix")
            if (
                docker("inspect", "--format", '{{index .Config.Labels "ontology.cdc-test-run"}}', name)
                != run_id
            ):
                raise RuntimeError("Test cleanup ownership changed")
            docker("rm", "--force", name)


def data_events(state, operation=None):
    return [
        event
        for event in state["events"]()
        if event["event_kind"] == "data" and (operation is None or event["operation"] == operation)
    ]


def assert_snapshot_preserves_keys_schema_precision_nulls_and_offset(live_cdc):
    state = live_cdc
    rows = eventually(
        lambda: data_events(state, "r") if len(data_events(state, "r")) == 2 else None,
        description="two real snapshot rows",
    )
    row = next(event for event in rows if event["key"]["payload"]["id"] == 1)
    assert row["snapshot"] in {"first", "true", "last", True}
    assert row["value"]["payload"]["after"]["raw"] == "AP8="
    assert row["value"]["payload"]["after"]["optional"] is None
    assert row["source_partition"] == {"server": "ontology_test"}
    assert isinstance(row["source_offset"]["lsn"], int)
    fields = row["value"]["schema"]["fields"][1]["fields"]
    amount = next(field for field in fields if field["field"] == "amount")
    assert amount["name"] == "org.apache.kafka.connect.data.Decimal"
    assert amount["parameters"]["scale"] == "18"
    assert hashlib.sha256(row["identity_material"].encode()).hexdigest() == row["event_id"]
    eventually(
        lambda: (state["directory"] / "offsets" / "offsets.dat").exists(),
        description="acknowledged persistent offset",
    )


def assert_transaction_same_key_updates_pk_change_delete_and_tombstone(live_cdc):
    state = live_cdc
    execute(state["conn"], "BEGIN")
    execute(state["conn"], "UPDATE items SET name='更新一次' WHERE id=1")
    execute(state["conn"], "UPDATE items SET name='更新一次' WHERE id=1")
    execute(state["conn"], "INSERT INTO items VALUES (3,'待删除',NULL,NULL,NULL)")
    execute(state["conn"], "COMMIT")
    updates = eventually(lambda: data_events(state, "u") if len(data_events(state, "u")) >= 2 else None)
    assert updates[-1]["event_id"] != updates[-2]["event_id"]
    # This pinned PG connector encodes transaction.id as txId:CURRENT_LSN.
    # A transaction must be grouped by native BEGIN/COMMIT and source txId,
    # not by equality of that changing display identifier.
    assert updates[-1]["source_offset"]["txId"] == updates[-2]["source_offset"]["txId"]
    assert updates[-1]["source_offset"]["ts_usec"] == updates[-2]["source_offset"]["ts_usec"]
    assert updates[-1]["transaction"]["total_order"] != updates[-2]["transaction"]["total_order"]
    txid = updates[-1]["source_offset"]["txId"]
    boundaries = eventually(
        lambda: [
            e
            for e in state["events"]()
            if e["event_kind"] == "transaction"
            and e["source_offset"]["txId"] == txid
            and e["transaction"]["status"] == "END"
        ]
    )
    assert boundaries[0]["transaction"]["event_count"] == 3
    execute(state["conn"], "UPDATE items SET id=30 WHERE id=3")
    execute(state["conn"], "DELETE FROM items WHERE id=30")
    deletes = eventually(lambda: data_events(state, "d") if len(data_events(state, "d")) >= 2 else None)
    assert {e["key"]["payload"]["id"] for e in deletes} >= {3, 30}
    assert any(h["key"] == "__debezium.newkey" for e in deletes for h in e["headers"])
    tombstones = eventually(lambda: [e for e in state["events"]() if e["event_kind"] == "tombstone"])
    assert tombstones[0]["value"] == {"schema": None, "payload": None}


def assert_restart_after_commit_before_ack_deduplicates_same_source_event(live_cdc):
    state, bridge = live_cdc, live_cdc["bridge"]
    # Allow preceding ACKs/offset flushes to settle, then lose the next committed response.
    eventually(
        lambda: any(
            e["event_kind"] == "tombstone" and e["key"]["payload"]["id"] == 30 for e in state["events"]()
        )
    )
    time.sleep(0.5)
    bridge.drop_after_commit = True
    execute(state["conn"], "INSERT INTO items VALUES (40,'重启恢复',NULL,NULL,NULL)")
    assert bridge.committed_without_ack.wait(30)
    with bridge.lock:
        lost_batch = bridge.calls[-1]["batch"]
        lost_ids = [e["event_id"] for e in lost_batch["events"]]
    state["remove"]()  # hard kill, do not advance Debezium offsets after missing ACK
    bridge.release_response.set()
    # Test-only expiry avoids a 60s sleep; production takeover waits for real lease expiry.
    with state["app"].state.db.transaction(state["account"]["tenant_id"]) as session:
        session.get(CdcStream, state["stream"]["id"]).lease_until = 0
    before = len(state["events"]())
    state["start"]()
    replay = eventually(
        lambda: [
            call
            for call in bridge.calls
            if call["status"] == 200
            and call["response"].get("inserted") == 0
            and any(e["event_id"] in lost_ids for e in call["batch"]["events"])
        ],
        timeout=60,
        description="real WAL replay deduplicated by durable inbox",
    )
    assert replay
    eventual_rows = eventually(
        lambda: [e for e in data_events(state, "c") if e["key"]["payload"]["id"] == 40]
    )
    assert len(eventual_rows) == 1
    assert len(state["events"]()) >= before
    assert not [call for call in bridge.calls if call["status"] == 409]
    execute(state["conn"], "INSERT INTO items VALUES (41,'恢复后继续',NULL,NULL,NULL)")
    eventually(lambda: [e for e in data_events(state, "c") if e["key"]["payload"]["id"] == 41])


def assert_unacked_batch_does_not_advance_offset_schema_and_truncate_are_visible(live_cdc):
    state, bridge = live_cdc, live_cdc["bridge"]
    time.sleep(0.5)
    offset_path = state["directory"] / "offsets" / "offsets.dat"
    old_offset = offset_path.read_bytes()
    bridge.reject_before_commit = True
    execute(state["conn"], "ALTER TABLE items ADD COLUMN added TEXT")
    execute(state["conn"], "UPDATE items SET added='新增字段' WHERE id=1")
    time.sleep(1.5)
    assert offset_path.read_bytes() == old_offset
    assert not any("added" in e["value"]["payload"].get("after", {}) for e in data_events(state, "u"))
    bridge.reject_before_commit = False
    changed = eventually(
        lambda: [
            e
            for e in data_events(state, "u")
            if e["value"]["payload"].get("after", {}).get("added") == "新增字段"
        ]
    )
    assert any(field["field"] == "added" for field in changed[-1]["value"]["schema"]["fields"][1]["fields"])
    execute(state["conn"], "TRUNCATE items")
    truncated = eventually(lambda: data_events(state, "t"))
    assert truncated[-1]["key"]["payload"] is None


def assert_one_event_chunks_replay_after_durable_prefix_and_rejected_suffix(state):
    bridge = state["bridge"]
    time.sleep(0.5)
    bridge.pause_after_next_ack = True
    bridge.rejected.clear()
    execute(state["conn"], "INSERT INTO items(id,name) VALUES (45,'部分确认恢复')")
    assert bridge.rejected.wait(30)
    with bridge.lock:
        denied_ids = {event["event_id"] for event in bridge.rejected_calls[-1]["events"]}
    assert not denied_ids.intersection(event["event_id"] for event in state["events"]())
    offset_path = state["directory"] / "offsets" / "offsets.dat"
    prior = offset_path.read_bytes()
    time.sleep(0.7)
    assert offset_path.read_bytes() == prior
    state["remove"]()
    bridge.reject_before_commit = False
    with state["app"].state.db.transaction(state["account"]["tenant_id"]) as session:
        session.get(CdcStream, state["stream"]["id"]).lease_until = 0
    state["start"]()
    eventually(
        lambda: denied_ids.issubset({event["event_id"] for event in state["events"]()}),
        timeout=60,
        description="unconfirmed one-event chunk replayed after hard kill",
    )
    eventually(lambda: [e for e in data_events(state, "c") if e["key"]["payload"]["id"] == 45])


def snapshot_notifications(state, status=None):
    return [
        event
        for event in state["events"]()
        if event["event_kind"] == "control"
        and event["value"]["schema"].get("name") == "io.debezium.connector.common.Notification"
        and event["value"]["payload"].get("aggregate_type") == "Initial Snapshot"
        and (status is None or event["value"]["payload"].get("type") == status)
    ]
