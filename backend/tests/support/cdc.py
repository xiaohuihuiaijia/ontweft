import hashlib
import json
from uuid import uuid4

from ontology.sources.models import Source

from tests.support.postgres import pg_databases as pg_databases  # noqa: PLC0414 - pytest fixture registration
from tests.support.postgres import pg_platform as pg_platform  # noqa: PLC0414 - pytest fixture registration


def setup_stream(platform):
    client, app, a, _ = platform
    with app.state.db.transaction(a["tenant_id"]) as session:
        source = Source(
            tenant_id=a["tenant_id"],
            project_id=a["project_id"],
            kind="postgres",
            name="CDC 合同来源",
            config={},
        )
        session.add(source)
        session.flush()
        source_id = source.id
    result = client.post("/api/cdc/streams", json={"source_id": source_id, "expected_source_revision": 1})
    assert result.status_code == 201, result.text
    stream, token = result.json()["stream"], result.json()["token"]
    assert "token_hash" not in stream
    headers = {"Authorization": f"Bearer {token}"}
    holder = str(uuid4())
    lease = client.post(f"/api/cdc/streams/{stream['id']}/lease", json={"holder_id": holder}, headers=headers)
    assert lease.status_code == 200, lease.text
    return (
        stream,
        headers,
        {
            "generation": stream["generation"],
            "holder_id": holder,
            "fencing_token": lease.json()["fencing_token"],
        },
    )


def native_event(generation, lsn=100, key=1):
    identity = {
        "identity_version": 1,
        "generation": generation,
        "source_partition": {"server": "test-source"},
        "source_position": {"lsn": lsn, "txId": 8, "ts_usec": 190000001, "messageType": "UPDATE"},
        "topic": "test.public.inventory",
        "event_kind": "data",
        "key_payload": {"id": key},
        "transaction": {"id": "8", "total_order": 1, "data_collection_order": 1},
        "operation": "u",
        "snapshot": "false",
    }
    material = json.dumps(identity, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return {
        "event_id": hashlib.sha256(material.encode()).hexdigest(),
        "identity_material": material,
        "event_kind": "data",
        "connector": "postgresql",
        "topic": identity["topic"],
        "kafka_partition": None,
        "timestamp": 999,
        "source_partition": identity["source_partition"],
        "source_offset": {**identity["source_position"], "lsn_proc": lsn, "lsn_commit": lsn - 1},
        "key": {
            "schema": {"type": "struct", "fields": [{"field": "id", "type": "int64"}]},
            "payload": {"id": key},
        },
        "value": {
            "schema": {"type": "struct"},
            "payload": {
                "before": {"name": "旧值"},
                "after": {"name": " 保留空格 ", "ts_ms": 12},
                "ts_ms": 999,
                "source": {"ts_ms": 500},
            },
        },
        "headers": [
            {"key": "same", "schema": {"type": "string"}, "value": "第一"},
            {"key": "same", "schema": {"type": "string"}, "value": "第二"},
        ],
        "operation": "u",
        "transaction": identity["transaction"],
        "snapshot": "false",
    }


def append(client, stream, headers, lease, events):
    return client.post(
        f"/api/cdc/streams/{stream['id']}/append", headers=headers, json={**lease, "events": events}
    )
