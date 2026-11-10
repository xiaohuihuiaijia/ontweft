"""Identity upgrades must preserve BSON document-key field order."""

from types import SimpleNamespace

import pytest
from ontology.connectors.mongo import MongoConnector
from ontology.core.domain import visible_evidence
from ontology.core.errors import DomainError
from ontology.sources.ingestion import fingerprint, resolve_identity
from ontology.sources.models import Source, SourceIdentity, SourceRecordRevision, SourceSnapshot

from tests.support.platform import publish


@pytest.mark.parametrize("change", ["key_order", "schema"])
def test_identity_order_or_schema_change_blocks_legacy_upgrade(platform, change):
    client, app, tenant, _ = platform
    publish(
        client,
        tenant["project_id"],
        {
            "types": [
                {
                    "key": "Record",
                    "label": "记录",
                    "properties": [{"key": "name", "label": "名称", "data_type": "string"}],
                }
            ]
        },
    )
    response = client.post(
        "/api/objects",
        json={"project_id": tenant["project_id"], "type_key": "Record", "properties": {"name": "原业务对象"}},
    )
    assert response.status_code == 201, response.text
    obj = response.json()
    old_key = {"_id": {"a": 1, "b": 2}}
    new_key = {"_id": {"b": 2, "a": 1}} if change == "key_order" else old_key.copy()
    old_hash = fingerprint(old_key)
    typed_key = {"_id": MongoConnector._identity(new_key["_id"])}
    assert old_hash == fingerprint(new_key)
    assert (MongoConnector._identity(old_key["_id"]) != typed_key["_id"]) == (change == "key_order")

    # Use the real SQLite schema with FK enforcement and valid tenant, project,
    # source, snapshot, raw record, entity and identity links. No Mongo mock.
    with app.state.db.transaction(tenant["tenant_id"]) as session:
        source = Source(
            tenant_id=tenant["tenant_id"],
            project_id=tenant["project_id"],
            name="历史 BSON 来源",
            kind="mongo",
            config={},
            secret="",
        )
        session.add(source)
        session.flush()
        snapshot = SourceSnapshot(
            tenant_id=tenant["tenant_id"],
            project_id=tenant["project_id"],
            source_id=source.id,
            source_revision=1,
            resource_id="records",
            schema_fingerprint="0" * 64,
            release_id=obj["release_id"],
            generation=1,
        )
        session.add(snapshot)
        session.flush()
        previous = SourceRecordRevision(
            tenant_id=tenant["tenant_id"],
            project_id=tenant["project_id"],
            snapshot_id=snapshot.id,
            attempt=1,
            source_key=old_key,
            identity_key=None,
            values={"name": "原业务对象"},
            key_hash=old_hash,
            content_hash="1" * 64,
        )
        session.add(previous)
        session.flush()
        identity = SourceIdentity(
            tenant_id=tenant["tenant_id"],
            project_id=tenant["project_id"],
            source_id=source.id,
            resource_id="records",
            type_key="Record",
            key_hash=old_hash,
            key_codec="legacy-json-v1",
            entity_id=obj["id"],
            last_record_id=previous.id,
            mapping_key="records",
        )
        session.add(identity)
        session.flush()
        current_snapshot = SourceSnapshot(
            tenant_id=tenant["tenant_id"],
            project_id=tenant["project_id"],
            source_id=source.id,
            source_revision=1,
            resource_id="records",
            schema_fingerprint=("2" if change == "schema" else "0") * 64,
            release_id=obj["release_id"],
            generation=2,
        )
        session.add(current_snapshot)
        session.flush()
        identity_id, record_id, snapshot_id = identity.id, previous.id, current_snapshot.id

    with pytest.raises(DomainError) as rejected, app.state.db.transaction(tenant["tenant_id"]) as session:
        current = SourceRecordRevision(
            source_key=new_key,
            identity_key=typed_key,
            key_hash=fingerprint({"codec": "typed-v1", "key": typed_key}),
        )
        resolve_identity(
            session,
            SimpleNamespace(tenant_id=tenant["tenant_id"]),
            session.get(SourceSnapshot, snapshot_id),
            {"type_key": "Record"},
            current,
        )
    assert rejected.value.status == 409
    with app.state.db.transaction(tenant["tenant_id"]) as session:
        identity = session.get(SourceIdentity, identity_id)
        assert identity.key_codec == "legacy-json-v1" and identity.key_hash == old_hash
        assert identity.entity_id == obj["id"] and identity.last_record_id == record_id
        previous = session.get(SourceRecordRevision, record_id)
        assert list(previous.source_key["_id"]) == ["a", "b"] and previous.identity_key is None


def test_full_source_row_hash_is_not_a_public_field_hash():
    reference = {
        "source_record_id": "source-row",
        "fields": ["name"],
        "content_hash": "hash-includes-unmapped-private-salary",
    }
    # Object views and Function input views share this helper. All mapped fields
    # being readable does not authorize hidden/unmapped columns of the source row.
    expected = [{"source_record_id": "source-row", "fields": ["name"]}]
    assert visible_evidence([reference], {"name"}) == expected
    assert reference["content_hash"] == "hash-includes-unmapped-private-salary"
    assert reference["fields"] == ["name"]
