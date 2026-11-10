import sqlite3
from decimal import Decimal

import pytest
from ontology.identity.models import Membership
from ontology.sources.models import SourceIdentity, SourceRecordRevision
from ontology.worker import process_one
from sqlalchemy import select

from tests.support.ingestion import capture, make_source
from tests.support.platform import publish


def test_worker_failure_before_snapshot_attempt_is_visible_without_logging_secrets(
    platform, monkeypatch, caplog
):
    from ontology.sources import ingestion

    client, app, owner, _ = platform
    source, resource, _, _ = make_source(client, app, owner["project_id"])
    result = client.post(
        f"/api/sources/{source['id']}/snapshots",
        json={"expected_source_revision": source["revision"], "resource_id": resource["resource_id"]},
    )
    assert result.status_code == 201, result.text
    secret = "private-driver-value-must-not-be-logged"

    def fail_before_attempt(*_):
        raise RuntimeError(secret)

    monkeypatch.setattr(ingestion, "run_snapshot", fail_before_attempt)
    assert process_one(app.state.db, app.state.settings)
    records = client.get(f"/api/snapshots/{result.json()['snapshot']['id']}/records").json()
    assert records["snapshot"]["status"] == "failed" and records["snapshot"]["attempt"] == 0
    assert records["items"] == [] and "RuntimeError" in caplog.text
    assert "fail_before_attempt" in caplog.text
    assert secret not in caplog.text and secret not in str(records)


def test_snapshot_projects_published_mapping_stable_identity_and_originals(platform):
    client, app, a, _ = platform
    source, resource, path, _ = make_source(client, app, a["project_id"])
    first = capture(client, app, source, resource)
    assert first["snapshot"]["status"] == "complete", first
    assert first["snapshot"]["projection"]["created"] == 2
    assert first["snapshot"]["consistency"] == "transaction"
    assert {row["source_key"]["id"] for row in first["items"]} == {1, 2}
    objects = client.get("/api/objects", params={"project_id": a["project_id"], "type_key": "Stock"}).json()[
        "items"
    ]
    obj = next(o for o in objects if o["properties"]["name"] == "A")
    assert obj["properties"]["quantity"] == 80
    manual = client.patch(
        f"/api/objects/{obj['id']}",
        json={"expected_revision": obj["revision"], "properties": {"note": "保留人工意见"}},
    )
    assert manual.status_code == 200, manual.text
    assert (
        client.patch(
            f"/api/objects/{obj['id']}",
            json={"expected_revision": manual.json()["revision"], "properties": {"quantity": 90}},
        ).status_code
        == 403
    )
    with sqlite3.connect(path) as connection:
        connection.execute("UPDATE inventory SET quantity='100' WHERE id=1")
    second = capture(client, app, source, resource)
    assert second["snapshot"]["status"] == "complete", second
    assert second["snapshot"]["projection"] == {
        "created": 0,
        "updated": 1,
        "unchanged": 1,
        "mappings": 1,
        "deleted": 0,
    }
    updated = client.get(f"/api/objects/{obj['id']}").json()
    assert updated["properties"] == {"name": "A", "quantity": 100, "note": "保留人工意见"}
    with app.state.db.transaction(a["tenant_id"]) as session:
        assert len(session.scalars(select(SourceRecordRevision)).all()) == 4
    assert client.get(f"/api/snapshots/{first['snapshot']['id']}/records").json()["items"] == first["items"]


def test_failed_mapping_keeps_raw_records_and_all_previous_objects(platform):
    client, app, a, _ = platform
    source, resource, path, _ = make_source(client, app, a["project_id"])
    capture(client, app, source, resource)
    before = client.get("/api/objects", params={"project_id": a["project_id"]}).json()
    with sqlite3.connect(path) as connection:
        connection.execute("UPDATE inventory SET quantity='120' WHERE id=1")
        connection.execute("UPDATE inventory SET quantity='not-a-number' WHERE id=2")
    failed = capture(client, app, source, resource)
    assert failed["snapshot"]["status"] == "failed", failed
    assert failed["total"] == 2
    assert client.get("/api/objects", params={"project_id": a["project_id"]}).json() == before


def test_schema_drift_blocks_projection_without_replacing_directory(platform):
    client, app, a, _ = platform
    source, resource, path, _ = make_source(client, app, a["project_id"])
    capture(client, app, source, resource)
    with sqlite3.connect(path) as connection:
        connection.execute("ALTER TABLE inventory ADD COLUMN warehouse TEXT")
    failed = capture(client, app, source, resource)
    assert failed["snapshot"]["status"] == "failed"
    assert "结构" in failed["snapshot"]["error"]
    stored = client.get("/api/sources", params={"project_id": a["project_id"]}).json()["items"][0]
    assert stored["resources"][0]["schema_fingerprint"] == resource["schema_fingerprint"]


def test_source_credentials_and_paths_are_validated_without_echo(platform):
    client, _, a, _ = platform
    payload = {
        "project_id": a["project_id"],
        "name": "bad",
        "kind": "postgres",
        "config": {"host": "example.invalid"},
    }
    assert client.post("/api/sources", json=payload).status_code == 422
    response = client.post("/api/sources", json={**payload, "config": {"password": "never-echo-this"}})
    assert response.status_code == 422 and "never-echo-this" not in response.text
    assert (
        client.post(
            "/api/sources",
            json={**payload, "kind": "sqlite", "config": {"database": "C:/Windows/system.ini"}},
        ).status_code
        == 422
    )


def test_new_snapshot_intent_prevents_old_projection_from_publishing(platform):
    client, app, a, _ = platform
    source, resource, _, _ = make_source(client, app, a["project_id"])
    payload = {"expected_source_revision": source["revision"], "resource_id": resource["resource_id"]}
    first = client.post(f"/api/sources/{source['id']}/snapshots", json=payload).json()["snapshot"]
    second = client.post(f"/api/sources/{source['id']}/snapshots", json=payload).json()["snapshot"]
    assert second["generation"] == first["generation"] + 1
    assert process_one(app.state.db, app.state.settings)
    old = client.get(f"/api/snapshots/{first['id']}/records").json()
    assert old["snapshot"]["status"] == "failed" and old["total"] == 2
    assert client.get("/api/objects", params={"project_id": a["project_id"]}).json()["total"] == 0
    assert process_one(app.state.db, app.state.settings)
    assert client.get(f"/api/snapshots/{second['id']}/records").json()["snapshot"]["status"] == "complete"


def test_removed_mapping_field_is_withdrawn_without_relabeling_old_value(platform):
    client, app, a, _ = platform
    source, resource, path, definition = make_source(client, app, a["project_id"])
    capture(client, app, source, resource)
    obj = client.get("/api/objects", params={"project_id": a["project_id"]}).json()["items"][0]
    definition["mappings"][0]["fields"].pop("quantity")
    publish(client, a["project_id"], definition)
    # A manual edit after publication must not lose the previous mapping lineage.
    client.patch(
        f"/api/objects/{obj['id']}",
        json={"expected_revision": obj["revision"], "properties": {"note": "仍保留"}},
    )
    with sqlite3.connect(path) as connection:
        connection.execute("UPDATE inventory SET quantity='999'")
    result = capture(client, app, source, resource)
    assert result["snapshot"]["status"] == "complete", result
    updated = client.get(f"/api/objects/{obj['id']}").json()
    assert "quantity" not in updated["properties"]
    assert updated["properties"]["note"] == "仍保留"
    assert updated["evidence"][0]["fields"] == ["name"]


def test_removing_live_mapping_requires_explicit_contribution_migration(platform):
    client, app, a, _ = platform
    source, resource, _, definition = make_source(client, app, a["project_id"])
    capture(client, app, source, resource)
    definition["mappings"] = []
    baseline = client.get("/api/ontology/definition", params={"project_id": a["project_id"]}).json()[
        "release_id"
    ]
    draft = client.post(
        "/api/changesets",
        json={
            "project_id": a["project_id"],
            "title": "撤映射",
            "definition": definition,
            "expected_baseline_release_id": baseline,
        },
    ).json()
    checked = client.post(f"/api/changesets/{draft['id']}/validate", json={"expected_revision": 1}).json()
    assert not checked["validation"]["valid"]
    assert any("贡献" in error["message"] for error in checked["validation"]["errors"])


def test_decimal_scale_changes_do_not_duplicate_projected_objects(platform, monkeypatch):
    from ontology.connectors.base import SourceRecord, canonical_identity_key
    from ontology.connectors.sql import SQLConnector

    client, app, a, _ = platform
    source, resource, _, _ = make_source(client, app, a["project_id"])
    scale = [".0"]

    def record(schema, values):
        key = {"id": Decimal(str(values["id"]) + scale[0])}
        return SourceRecord(key=key, identity_key=canonical_identity_key(key), values=values)

    monkeypatch.setattr(SQLConnector, "_record", staticmethod(record))
    first = capture(client, app, source, resource)
    before = client.get("/api/objects", params={"project_id": a["project_id"]}).json()["items"]
    scale[0] = ".00"
    second = capture(client, app, source, resource)
    assert second["snapshot"]["status"] == "complete", second
    assert second["snapshot"]["projection"]["created"] == 0
    assert first["items"][0]["source_key"] != second["items"][0]["source_key"]
    after = client.get("/api/objects", params={"project_id": a["project_id"]}).json()["items"]
    assert {item["id"] for item in before} == {item["id"] for item in after}


@pytest.mark.parametrize("changed_raw_key", [False, True])
def test_legacy_identity_upgrade_requires_exact_raw_evidence(platform, monkeypatch, changed_raw_key):
    from ontology.connectors.base import SourceRecord, canonical_identity_key
    from ontology.connectors.sql import SQLConnector

    client, app, a, _ = platform
    source, resource, _, _ = make_source(client, app, a["project_id"])
    mode = {"typed": False, "scale": ".0"}

    def record(schema, values):
        key = {"id": Decimal(str(values["id"]) + mode["scale"])}
        return SourceRecord(
            key=key, identity_key=canonical_identity_key(key) if mode["typed"] else None, values=values
        )

    monkeypatch.setattr(SQLConnector, "_record", staticmethod(record))
    first = capture(client, app, source, resource)
    before = client.get("/api/objects", params={"project_id": a["project_id"]}).json()
    mode.update(typed=True, scale=".00" if changed_raw_key else ".0")
    second = capture(client, app, source, resource)
    assert second["snapshot"]["status"] == ("failed" if changed_raw_key else "complete"), second
    assert client.get("/api/objects", params={"project_id": a["project_id"]}).json() == before
    if changed_raw_key:
        assert "审核身份迁移" in second["snapshot"]["error"]
    with app.state.db.transaction(a["tenant_id"]) as session:
        identities = list(session.scalars(select(SourceIdentity)))
        assert len(identities) == 2
        assert {item.key_codec for item in identities} == {
            "legacy-json-v1" if changed_raw_key else "typed-v1"
        }
    assert client.get(f"/api/snapshots/{first['snapshot']['id']}/records").json()["items"] == first["items"]


def test_field_permission_filters_source_evidence_and_history(platform):
    client, app, a, _ = platform
    source, resource, _, definition = make_source(client, app, a["project_id"])
    definition["types"][0]["properties"][1]["read_roles"] = ["owner"]
    publish(client, a["project_id"], definition)
    captured = capture(client, app, source, resource)
    assert captured["snapshot"]["status"] == "complete", captured
    obj = client.get("/api/objects", params={"project_id": a["project_id"]}).json()["items"][0]
    with app.state.db.transaction(a["tenant_id"]) as session:
        membership = session.scalar(select(Membership).where(Membership.user_id == a["user_id"]))
        membership.role = "member"
    visible = client.get(f"/api/objects/{obj['id']}").json()
    history = client.get(f"/api/objects/{obj['id']}/history").json()["items"]
    for row in [visible, *history]:
        assert "quantity" not in row["properties"]
        assert row["evidence"][0]["fields"] == ["name"]
        assert "content_hash" not in row["evidence"][0]
    assert client.get(f"/api/source-records/{obj['evidence'][0]['source_record_id']}").status_code == 403


def test_snapshot_cancel_and_retry_have_matching_visible_status(platform):
    client, app, a, _ = platform
    source, resource, _, _ = make_source(client, app, a["project_id"])
    queued = client.post(
        f"/api/sources/{source['id']}/snapshots",
        json={"expected_source_revision": source["revision"], "resource_id": resource["resource_id"]},
    ).json()
    job_id, snapshot_id = queued["job"]["id"], queued["snapshot"]["id"]
    for operation, expected in [("cancel", "cancelled"), ("retry", "pending")]:
        assert client.post(f"/api/jobs/{job_id}/{operation}").status_code == 200
        result = client.get(f"/api/snapshots/{snapshot_id}/records").json()
        assert result["snapshot"]["status"] == expected
    assert process_one(app.state.db, app.state.settings)
    assert client.get(f"/api/snapshots/{snapshot_id}/records").json()["snapshot"]["status"] == "complete"
