"""A real model publication must retain evidence after value normalization."""

from copy import deepcopy

from ontology.modeling.models import Release

from tests.support.contribution_decisions import (
    conflict_fixture,
    context,
    decide,
    ledger_snapshot,
    path,
    preview,
    source_version,
)
from tests.support.contribution_decisions import decision_platform as decision_platform  # noqa: PLC0414 - pytest fixture registration
from tests.support.platform import publish
from tests.support.postgres import pg_databases as pg_databases  # noqa: PLC0414 - pytest fixture registration
from tests.support.postgres import pg_platform as pg_platform  # noqa: PLC0414 - pytest fixture registration


def test_string_to_integer_redecision_preserves_selected_original_evidence(decision_platform):
    client, app, owner, _ = decision_platform
    ids = conflict_fixture(app, owner)
    # Both retained current contributions are integer-compatible strings. This
    # is a new immutable source record, not an edit of the prior raw evidence.
    ids["records"][0] = source_version(app, owner, ids, 0, values={"name": "A", "quantity": "10"})
    with app.state.db.transaction(owner["tenant_id"]) as session:
        definition = deepcopy(session.get(Release, ids["release"]).definition)
    quantity = next(prop for prop in definition["types"][0]["properties"] if prop["key"] == "quantity")
    quantity["data_type"] = "string"
    publish(client, owner["project_id"], definition)
    original_sources = ledger_snapshot(app, owner)

    selected_as_string = decide(client, ids, selected=1)
    assert selected_as_string["object"]["properties"]["quantity"] == "12"
    string_release = context(client, ids)["expected_model_release_id"]

    quantity["data_type"] = "integer"
    publish(client, owner["project_id"], definition)
    current = context(client, ids)
    assert current["expected_model_release_id"] != string_release
    assert current["data_status"] == "conflict"
    field = next(item for item in current["fields"] if item["field_key"] == "quantity")
    assert field["state"] == "stale" and field["current_present"] is False
    assert {item["value"] for item in field["candidates"]} == {"10", "12"}

    body, proposed = preview(client, ids, selected=1, reason="按新整数类型重新核对同一原始凭据")
    assert type(proposed["after"]["value"]) is int
    assert proposed["after"]["value"] == 12
    response = client.post(path(ids), json=body)
    assert response.status_code == 201, response.text
    committed = response.json()
    reread = client.get(f"/api/objects/{ids['entity']}")
    assert reread.status_code == 200, reread.text
    for obj in (committed["object"], reread.json()):
        assert obj["data_status"] == "active"
        assert type(obj["properties"]["quantity"]) is int and obj["properties"]["quantity"] == 12
        supporting = [
            ref
            for ref in obj["evidence"]
            if "source_record_id" in ref and "quantity" in ref.get("fields", [])
        ]
        assert len(supporting) == 1
        assert supporting[0]["source_record_id"] == ids["records"][1]
        assert supporting[0]["contribution_id"] == ids["contributions"][1]
        assert all(ref["source_record_id"] != ids["records"][0] for ref in supporting)
        # The other source still supports the agreed name; it cannot support
        # quantity merely because it belongs to the same materialized object.
        assert any(
            ref.get("source_record_id") == ids["records"][0]
            and "name" in ref.get("fields", [])
            and "quantity" not in ref.get("fields", [])
            for ref in obj["evidence"]
        )
        assert any(
            ref.get("contribution_decision_id") == committed["decision"]["id"]
            and ref.get("decision_state") == "selected"
            and ref.get("fields") == ["quantity"]
            for ref in obj["evidence"]
        )
    assert committed["decision"]["selected_contribution_revision"] == 1
    final_sources = ledger_snapshot(app, owner)
    for table in (
        "source_record_revisions",
        "source_identities",
        "source_contributions",
        "contribution_revisions",
    ):
        assert final_sources[table] == original_sources[table]
