"""A new generation replaces a proved baseline without erasing old evidence."""

from uuid import uuid4

import pytest
from sqlalchemy import select

from ontology.cdc.models import CdcEvent, CdcStream
from ontology.modeling.models import Release
from ontology.objects.models import Entity
from tests.support.cdc_projection import setup_projection, append_events, fixtures, run, view
from tests.support.platform import publish
from tests.support.postgres import pg_databases as pg_databases  # noqa: PLC0414
from tests.support.postgres import pg_platform as pg_platform  # noqa: PLC0414


@pytest.mark.parametrize("fixture_name", ["platform", "pg_platform"])
def test_rebuild_retains_history_invalidates_old_token_and_requires_complete_baseline(
    request, fixture_name, monkeypatch
):
    platform = request.getfixturevalue(fixture_name)
    client, app, owner, _ = platform
    old = setup_projection(platform, monkeypatch)
    native = fixtures()
    append_events(old, native[:6])
    assert run(old)["status"] == "caught_up"
    with app.state.db.transaction(owner["tenant_id"]) as session:
        original = {e.id: dict(e.properties) for e in session.scalars(select(Entity))}
    payload = dict(
        expected_binding_id=old["binding_id"],
        expected_release_id=old["release"],
        expected_source_revision=1,
        resource_ids=[r["resource_id"] for r in old["resources"]],
    )
    url = f"/api/cdc/streams/{old['stream']['id']}/rebuild"
    invalid = client.post(url, json={**payload, "expected_source_revision": 2})
    assert invalid.status_code == 409
    assert client.get(f"/api/cdc/streams/{old['stream']['id']}/events").json()["stream"]["state"] == "active"
    rebuilt = client.post(url, json=payload)
    assert rebuilt.status_code == 201, rebuilt.text
    result = rebuilt.json()
    assert client.post(url, json=payload).status_code == 409
    assert client.post(
        f"/api/cdc/streams/{old['stream']['id']}/lease",
        json={"holder_id": str(uuid4())},
        headers=old["headers"],
    ).status_code in {401, 409}
    assert client.post(f"/api/cdc/streams/{old['stream']['id']}/resume", json={}).status_code == 409
    new = {**old, "stream": result["stream"], "headers": {"Authorization": "Bearer " + result["token"]}}
    holder = str(uuid4())
    lease = client.post(
        f"/api/cdc/streams/{new['stream']['id']}/lease", json={"holder_id": holder}, headers=new["headers"]
    ).json()
    new["lease"] = dict(
        generation=new["stream"]["generation"], holder_id=holder, fencing_token=lease["fencing_token"]
    )
    new["binding_id"] = view(new)["binding"]["id"]
    append_events(new, native[:5])
    assert run(new)["status"] == "waiting"
    with app.state.db.transaction(owner["tenant_id"]) as session:
        assert {e.id: e.properties for e in session.scalars(select(Entity))} == original
    append_events(new, native[5:6])
    assert run(new)["status"] == "caught_up"
    with app.state.db.transaction(owner["tenant_id"]) as session:
        assert {e.id: e.properties for e in session.scalars(select(Entity))} == original
        assert session.get(CdcStream, old["stream"]["id"]).state == "retired"
        assert session.get(CdcStream, new["stream"]["id"]).previous_stream_id == old["stream"]["id"]
        assert (
            len(session.scalars(select(CdcEvent).where(CdcEvent.stream_id == old["stream"]["id"])).all()) == 6
        )


def test_unrelated_model_release_does_not_require_new_capture(platform, monkeypatch):
    client, app, owner, _ = platform
    state = setup_projection(platform, monkeypatch)
    with app.state.db.transaction(owner["tenant_id"]) as session:
        definition = session.get(Release, state["release"]).definition
    definition = {
        **definition,
        "types": [*definition["types"], {"key": "Unrelated", "label": "Other", "properties": []}],
    }
    definition["types"][0] = {**definition["types"][0], "label": "Renamed display label"}
    current = publish(client, owner["project_id"], definition)
    assert current["release_id"] != state["release"]
    assert view(state)["reason_code"] != "binding_changed"
    append_events(state, fixtures()[:6])
    assert run(state)["status"] == "caught_up"


def test_legacy_key_default_and_field_order_are_not_capture_contract_changes(monkeypatch):
    import copy
    from types import SimpleNamespace
    from ontology.cdc import projection

    previous = {
        "mappings": [{"key": "m", "source_id": "source", "type_key": "Item"}],
        "types": [{"key": "Item", "properties": [{"key": "a"}, {"key": "b"}]}],
    }
    current = copy.deepcopy(previous)
    current["types"][0].update(key_semantics="legacy", label="Display rename")
    current["types"][0]["properties"].reverse()
    monkeypatch.setattr(projection, "active_definition", lambda *_: current)
    session = SimpleNamespace(get=lambda *_: SimpleNamespace(definition=previous))
    project = SimpleNamespace(active_release_id="new")
    binding = SimpleNamespace(release_id="old", source_id="source")
    assert projection.binding_matches_model(session, project, binding)
    current["types"][0]["key_semantics"] = "canonical"
    assert not projection.binding_matches_model(session, project, binding)
