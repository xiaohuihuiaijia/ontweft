"""Required source NULL is data quality, not withdrawal or a global schema relaxation."""

import copy
import sqlite3
from types import SimpleNamespace

import pytest
from ontology.cdc.decode import ValueState
from ontology.cdc.values import decode_row
from ontology.core.errors import DomainError
from ontology.objects.contributions.models import ContributionRevision, SourceContribution
from ontology.objects.contributions.service import ensure_legacy_contributions
from ontology.objects.models import Entity
from ontology.sources.ingestion import mapped_properties
from ontology.sources.models import Source, SourceIdentity, SourceRecordRevision
from sqlalchemy import select

from tests.support.cdc_projection import append_events, fixtures, run, setup_projection
from tests.support.contributions import apply, loaded, raw_record, seed
from tests.support.ingestion import capture
from tests.support.platform import publish
from tests.support.postgres import pg_databases as pg_databases  # noqa: PLC0414 - pytest fixture registration
from tests.support.postgres import pg_platform as pg_platform  # noqa: PLC0414 - pytest fixture registration


def required_type():
    return {
        "key": "Item",
        "properties": [
            {"key": "name", "data_type": "string", "required": True, "source_owned": True},
            {"key": "manual", "data_type": "string", "required": True},
        ],
    }


def test_incomplete_source_mapping_is_opt_in_and_does_not_mutate_required_contract():
    definition = required_type()
    original = copy.deepcopy(definition)
    mapping = {"fields": {"name": {"source_field": "name", "transform": "trim"}}}
    record = SimpleNamespace(id="synthetic", values={"name": None})
    for options in ({}, {"allow_incomplete": False}):
        with pytest.raises(DomainError) as error:
            mapped_properties(record, mapping, definition, **options)
        assert error.value.code == "invalid_properties"
    assert mapped_properties(record, mapping, definition, allow_incomplete=True) == {"name": None}
    assert definition == original
    # A manual required field is not relaxed even in the explicit source path.
    manual_mapping = {"fields": {"manual": {"source_field": "name", "transform": "identity"}}}
    with pytest.raises(DomainError) as error:
        mapped_properties(record, manual_mapping, definition, allow_incomplete=True)
    assert error.value.code == "invalid_properties"


def test_incomplete_source_mapping_does_not_invent_missing_values_or_accept_wrong_types():
    definition = required_type()
    mapping = {"fields": {"name": {"source_field": "name", "transform": "identity"}}}
    for values, code in (({}, "missing_source_field"), ({"name": 123}, "invalid_properties")):
        with pytest.raises(DomainError) as error:
            mapped_properties(
                SimpleNamespace(id="synthetic", values=values), mapping, definition, allow_incomplete=True
            )
        assert error.value.code == code


def test_manual_object_creation_and_patch_still_enforce_required_values(platform):
    client, _, owner, _ = platform
    publish(
        client,
        owner["project_id"],
        {
            "types": [
                {
                    "key": "Manual",
                    "label": "Manual",
                    "properties": [
                        {"key": "name", "label": "Name", "data_type": "string", "required": True},
                    ],
                }
            ]
        },
    )
    for properties in ({}, {"name": None}):
        response = client.post(
            "/api/objects",
            json={"project_id": owner["project_id"], "type_key": "Manual", "properties": properties},
        )
        assert response.status_code == 422
    response = client.post(
        "/api/objects",
        json={"project_id": owner["project_id"], "type_key": "Manual", "properties": {"name": "valid"}},
    )
    assert response.status_code == 201
    entity = response.json()
    invalid = client.patch(
        f"/api/objects/{entity['id']}",
        json={"expected_revision": entity["revision"], "properties": {"name": None}},
    )
    assert invalid.status_code == 422
    assert client.get(f"/api/objects/{entity['id']}").json()["properties"] == {"name": "valid"}


def test_real_sqlite_snapshot_null_materializes_incomplete_and_later_recovers(platform):
    client, app, owner, _ = platform
    imports = app.state.settings.data_dir / "imports"
    imports.mkdir(parents=True)
    path = imports / "nullable.db"
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE items(id INTEGER PRIMARY KEY, name TEXT)")
        connection.execute("INSERT INTO items VALUES(1, NULL)")
    response = client.post(
        "/api/sources",
        json={
            "project_id": owner["project_id"],
            "name": "Nullable source",
            "kind": "sqlite",
            "config": {"database": str(path)},
        },
    )
    assert response.status_code == 201
    source = response.json()
    assert client.post(f"/api/sources/{source['id']}/test").json()["ok"]
    discovery = client.post(f"/api/sources/{source['id']}/discover")
    assert discovery.status_code == 200
    resource = discovery.json()["resources"][0]
    definition = {
        "types": [
            {
                "key": "Item",
                "label": "Item",
                "properties": [
                    {
                        "key": "name",
                        "label": "Name",
                        "data_type": "string",
                        "required": True,
                        "source_owned": True,
                    },
                    {"key": "note", "label": "Manual", "data_type": "string"},
                ],
            }
        ],
        "mappings": [
            {
                "key": "items",
                "source_id": source["id"],
                "resource_id": resource["resource_id"],
                "schema_fingerprint": resource["schema_fingerprint"],
                "type_key": "Item",
                "fields": {"name": {"source_field": "name", "transform": "trim"}},
            }
        ],
    }
    publish(client, owner["project_id"], definition)
    first = capture(client, app, source, resource)
    assert first["snapshot"]["status"] == "complete", first["snapshot"]
    with app.state.db.transaction(owner["tenant_id"]) as session:
        entity = session.scalar(select(Entity))
        entity_id = entity.id
        contribution = session.scalar(select(SourceContribution))
        record = session.get(SourceRecordRevision, contribution.record_id)
        assert entity.data_status == "incomplete" and entity.properties == {"name": None}
        assert contribution.active and contribution.withdraw_reason is None
        assert contribution.mapped_values == {"name": None}
        assert record.values["name"] is None
    with sqlite3.connect(path) as connection:
        connection.execute("UPDATE items SET name='recovered' WHERE id=1")
    assert capture(client, app, source, resource)["snapshot"]["status"] == "complete"
    recovered = client.get(f"/api/objects/{entity_id}").json()
    assert recovered["data_status"] == "active" and recovered["properties"]["name"] == "recovered"
    changed = client.patch(
        f"/api/objects/{entity_id}",
        json={"expected_revision": recovered["revision"], "properties": {"note": "keep manual"}},
    )
    assert changed.status_code == 200
    with sqlite3.connect(path) as connection:
        connection.execute("UPDATE items SET name=NULL WHERE id=1")
    assert capture(client, app, source, resource)["snapshot"]["status"] == "complete"
    cleared = client.get(f"/api/objects/{entity_id}").json()
    assert cleared["data_status"] == "incomplete"
    assert cleared["properties"] == {"name": None, "note": "keep manual"}
    # An existing incomplete source object must not block an unrelated model
    # label edit, but non-null/manual values still constrain type migrations.
    unrelated = copy.deepcopy(definition)
    unrelated["types"][0]["label"] = "Renamed Item"
    released = publish(client, owner["project_id"], unrelated)
    incompatible = copy.deepcopy(unrelated)
    incompatible["types"][0]["properties"][1]["data_type"] = "integer"
    draft = client.post(
        "/api/changesets",
        json={
            "project_id": owner["project_id"],
            "title": "Invalid manual type",
            "definition": incompatible,
            "expected_baseline_release_id": released["release_id"],
        },
    )
    assert draft.status_code == 201
    checked = client.post(
        f"/api/changesets/{draft.json()['id']}/validate", json={"expected_revision": draft.json()["revision"]}
    )
    assert checked.status_code == 200 and not checked.json()["validation"]["valid"]


@pytest.mark.parametrize("fixture_name", ["platform", "pg_platform"])
def test_null_contribution_conflicts_with_value_then_becomes_incomplete_when_other_source_withdraws(
    request, fixture_name
):
    _, app, owner, _ = request.getfixturevalue(fixture_name)
    ids = seed(app.state.db, owner)
    with app.state.db.transaction(owner["tenant_id"]) as session:
        ctx, entity, identity, release = loaded(session, owner, ids)
        ensure_legacy_contributions(session, ctx, entity)
        source2 = session.get(Source, ids["sources"][1])
        record2 = raw_record(session, owner, source2, release, {"name": None, "quantity": "10"})
        identity2 = SourceIdentity(
            tenant_id=owner["tenant_id"],
            project_id=owner["project_id"],
            source_id=source2.id,
            resource_id="inventory",
            type_key="Stock",
            entity_id=entity.id,
            key_hash=record2.key_hash,
            last_record_id=record2.id,
            mapping_key="mapping1",
        )
        session.add(identity2)
        session.flush()
        result = apply(
            session, ctx, identity2, record2, release, index=1, values=record2.values, new_identity=True
        )
        assert result["contribution"].active and result["contribution"].mapped_values["name"] is None
        assert entity.data_status == "conflict" and "name" not in entity.properties
        apply(
            session,
            ctx,
            identity,
            session.get(SourceRecordRevision, ids["record"]),
            release,
            withdraw_reason="first source removed",
        )
        assert entity.data_status == "incomplete" and entity.properties["name"] is None
        assert entity.properties["note"] == "manual retained"
        assert session.scalar(
            select(SourceContribution).where(SourceContribution.identity_id == identity2.id)
        ).active
        recovered = raw_record(
            session, owner, source2, release, {"name": "back", "quantity": "10"}, generation=2
        )
        apply(session, ctx, identity2, recovered, release, index=1, values=recovered.values)
        assert entity.data_status == "active" and entity.properties["name"] == "back"
        history = session.scalars(
            select(ContributionRevision)
            .where(ContributionRevision.identity_id == identity2.id)
            .order_by(ContributionRevision.revision)
        ).all()
        assert [revision.mapped_values["name"] for revision in history] == [None, "back"]


def test_legacy_bootstrap_stays_strict_and_never_invents_required_null(platform):
    _, app, owner, _ = platform
    ids = seed(app.state.db, owner)
    with app.state.db.transaction(owner["tenant_id"]) as session:
        _, entity, identity, release = loaded(session, owner, ids)
        record = raw_record(
            session,
            owner,
            session.get(Source, ids["sources"][0]),
            release,
            {"name": None, "quantity": "10"},
            generation=2,
        )
        identity.last_record_id = record.id
        entity.properties = {"name": None, "quantity": "10", "note": "manual retained"}
    with pytest.raises(DomainError) as error, app.state.db.transaction(owner["tenant_id"]) as session:
        ctx, entity, _, _ = loaded(session, owner, ids)
        ensure_legacy_contributions(session, ctx, entity)
    assert error.value.code == "invalid_properties"
    with app.state.db.transaction(owner["tenant_id"]) as session:
        assert session.scalar(select(SourceContribution)) is None


@pytest.mark.parametrize("fixture_name", ["platform", "pg_platform"])
def test_native_cdc_required_null_commits_incomplete_then_recovers_and_clears(
    request, fixture_name, monkeypatch
):
    platform = request.getfixturevalue(fixture_name)

    def publish_required(client, project_id, definition):
        definition = copy.deepcopy(definition)
        next(prop for prop in definition["types"][0]["properties"] if prop["key"] == "optional")[
            "required"
        ] = True
        return publish(client, project_id, definition)

    # Only the fixture definition is specialized; publishing, inbox, leased job,
    # immutable evidence, contribution writes and object API remain real.
    monkeypatch.setattr("tests.support.cdc_projection.publish", publish_required)
    state = setup_projection(platform, monkeypatch)
    native = fixtures("pg-toast-events.json")
    append_events(state, native[:6])
    result = run(state, automatic=True)
    assert result["status"] == "caught_up" and result["projected_sequence"] == 6, result
    client, app, owner, _ = platform
    with app.state.db.transaction(owner["tenant_id"]) as session:
        entities = session.scalars(select(Entity)).all()
        entity_id = next(entity.id for entity in entities if entity.properties["source_id"] == 1)
    assert client.get(f"/api/objects/{entity_id}").json()["data_status"] == "incomplete"
    for offset, expected_status in ((6, "active"), (9, "active"), (12, "incomplete")):
        append_events(state, native[offset : offset + 3])
        result = run(state, automatic=True)
        assert result["status"] == "caught_up" and result["projected_sequence"] == offset + 3, result
        entity = client.get(f"/api/objects/{entity_id}").json()
        assert entity["data_status"] == expected_status
    with app.state.db.transaction(owner["tenant_id"]) as session:
        identity = session.scalar(select(SourceIdentity).where(SourceIdentity.entity_id == entity_id))
        contribution = session.scalar(
            select(SourceContribution).where(SourceContribution.identity_id == identity.id)
        )
        assert contribution.active and contribution.withdraw_reason is None
        assert contribution.mapped_values["optional"] is None
        assert decode_row(contribution.native_row).fields["optional"].state == ValueState.NULL
