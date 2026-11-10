import copy
import hashlib
import json
import time
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import pytest
from ontology.cdc.models import CdcEvent, CdcStream
from ontology.identity.models import Membership
from sqlalchemy import event, select, text
from sqlalchemy.exc import DBAPIError

from tests.support.cdc import append, native_event, setup_stream
from tests.support.postgres import pg_databases as pg_databases  # noqa: PLC0414 - pytest fixture registration
from tests.support.postgres import pg_platform as pg_platform  # noqa: PLC0414 - pytest fixture registration


@pytest.mark.parametrize("fixture_name", ["platform", "pg_platform"])
def test_durable_receipt_replay_preserves_native_record_and_separate_watermarks(request, fixture_name):
    platform = request.getfixturevalue(fixture_name)
    client, app, a, _ = platform
    stream, headers, lease = setup_stream(platform)
    original = native_event(stream["generation"], key="中文键 ")
    original["value"]["payload"]["after"]["external_id"] = 9223372036854775807
    response = append(client, stream, headers, lease, [original])
    assert response.status_code == 200, response.text
    assert response.json()["received_sequence"] == 1
    assert response.json()["projected_sequence"] == response.json()["indexed_sequence"] == 0
    replay = copy.deepcopy(original)
    replay["timestamp"] = replay["value"]["payload"]["ts_ms"] = 12345
    replay["source_offset"].update(lsn_proc=99999, lsn_commit=99998)
    response = append(client, stream, headers, lease, [replay, replay])
    assert response.status_code == 200, response.text
    assert response.json()["accepted_event_ids"] == [original["event_id"], original["event_id"]]
    assert response.json()["inserted"] == 0
    reformatted = copy.deepcopy(replay)
    identity = json.loads(reformatted["identity_material"])
    reformatted["identity_material"] = json.dumps(dict(reversed(list(identity.items()))), indent=2)
    reformatted["event_id"] = hashlib.sha256(reformatted["identity_material"].encode()).hexdigest()
    assert reformatted["event_id"] != original["event_id"]
    response = append(client, stream, headers, lease, [reformatted])
    assert response.status_code == 200, response.text
    assert response.json()["accepted_event_ids"] == [reformatted["event_id"]]
    assert response.json()["inserted"] == 0 and response.json()["received_sequence"] == 1
    conflicting = copy.deepcopy(reformatted)
    conflicting["value"]["payload"]["after"]["name"] = "different business content"
    assert append(client, stream, headers, lease, [conflicting]).status_code == 409
    result = client.get(f"/api/cdc/streams/{stream['id']}/events").json()
    assert len(result["items"]) == 1 and result["items"][0]["record"] == original
    assert '"external_id":9223372036854775807' in result["items"][0]["record_json"]
    assert "token_hash" not in json.dumps(result)
    if app.state.db.engine.dialect.name == "postgresql":
        with pytest.raises(DBAPIError), app.state.db.transaction(a["tenant_id"]) as session:
            session.execute(text("DELETE FROM cdc_events"))
        with app.state.db.transaction() as session:
            assert session.scalars(select(CdcEvent)).all() == []


def test_conflicting_duplicate_rolls_back_whole_batch_and_checks_business_timestamps(platform):
    client, _, _, _ = platform
    stream, headers, lease = setup_stream(platform)
    original = native_event(stream["generation"])
    assert append(client, stream, headers, lease, [original]).status_code == 200
    changed = copy.deepcopy(original)
    changed["value"]["payload"]["after"]["ts_ms"] = 13
    response = append(client, stream, headers, lease, [native_event(stream["generation"], 101), changed])
    assert response.status_code == 409, response.text
    records = client.get(f"/api/cdc/streams/{stream['id']}/events").json()
    assert records["stream"]["received_sequence"] == len(records["items"]) == 1


def test_receipt_is_not_returned_when_database_commit_fails(platform):
    client, _, _, _ = platform
    stream, headers, lease = setup_stream(platform)
    from sqlalchemy.orm import Session

    def fail_commit(session):
        if any(isinstance(item, CdcStream) and item.received_sequence for item in session.dirty):
            raise RuntimeError("simulated commit failure")

    event.listen(Session, "before_commit", fail_commit)
    try:
        with pytest.raises(RuntimeError, match="simulated commit failure"):
            append(client, stream, headers, lease, [native_event(stream["generation"])])
    finally:
        event.remove(Session, "before_commit", fail_commit)
    records = client.get(f"/api/cdc/streams/{stream['id']}/events").json()
    assert records["items"] == [] and records["stream"]["received_sequence"] == 0
    assert (
        append(client, stream, headers, lease, [native_event(stream["generation"])]).json()[
            "received_sequence"
        ]
        == 1
    )


def test_takeover_rejects_old_writer_and_pause_preserves_generation(platform):
    client, app, a, _ = platform
    stream, headers, old = setup_stream(platform)
    holder = str(uuid4())
    path = f"/api/cdc/streams/{stream['id']}"
    assert client.post(path + "/lease", headers=headers, json={"holder_id": holder}).status_code == 409
    with app.state.db.transaction(a["tenant_id"]) as session:
        session.get(CdcStream, stream["id"]).lease_until = time.time() - 1
    response = client.post(path + "/lease", headers=headers, json={"holder_id": holder})
    assert response.json()["fencing_token"] > old["fencing_token"]
    assert append(client, stream, headers, old, [native_event(stream["generation"])]).status_code == 409
    assert client.post(path + "/pause").status_code == 200
    assert client.post(path + "/lease", headers=headers, json={"holder_id": holder}).status_code == 409
    resumed = client.post(path + "/resume").json()["stream"]
    assert resumed["generation"] == stream["generation"]
    rotated = client.post(path + "/rotate-token").json()
    assert client.post(path + "/lease", headers=headers, json={"holder_id": holder}).status_code == 401
    assert rotated["token"] != headers["Authorization"].removeprefix("Bearer ")


def test_machine_token_is_scoped_and_configurer_revocation_stops_receipt(platform):
    client, app, a, b = platform
    stream, headers, lease = setup_stream(platform)
    body = [native_event(stream["generation"])]
    assert append(client, stream, {}, lease, body).status_code == 401
    tampered = {"Authorization": headers["Authorization"].replace(a["tenant_id"], b["tenant_id"])}
    assert append(client, stream, tampered, lease, body).status_code == 401
    body[0]["source_offset"]["lsn"] += 1
    assert append(client, stream, headers, lease, body).status_code == 422
    with app.state.db.transaction(a["tenant_id"]) as session:
        session.scalar(select(Membership).where(Membership.user_id == a["user_id"])).role = "member"
    assert append(client, stream, headers, lease, [native_event(stream["generation"])]).status_code == 403
    assert client.get(f"/api/cdc/streams/{stream['id']}/events").status_code == 403


def test_postgres_simultaneous_duplicate_receipts_advance_once(request):
    platform = request.getfixturevalue("pg_platform")
    client, _, _, _ = platform
    stream, headers, lease = setup_stream(platform)
    original = native_event(stream["generation"])
    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(lambda _: append(client, stream, headers, lease, [original]), [1, 2]))
    assert [response.status_code for response in responses] == [200, 200]
    assert sorted(response.json()["inserted"] for response in responses) == [0, 1]
    assert {response.json()["received_sequence"] for response in responses} == {1}
