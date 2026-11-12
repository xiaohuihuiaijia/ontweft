import pytest

from ontology.core.domain import coerce_value
from ontology.objects.constraints import key_values, number_text
from ontology.objects.relation_matching import _family, _property, _value_key
from tests.support.platform import publish


@pytest.mark.parametrize("value", ["1e1000000000", "1e-1000000000", "9" * 1001])
def test_numeric_budget_rejects_expansion_before_rendering(value):
    with pytest.raises(ValueError, match="1000"):
        coerce_value({"data_type": "number"}, value)
    with pytest.raises(ValueError, match="1000"):
        number_text(value)


def test_exact_budget_and_equivalent_numbers():
    assert number_text("1e999") == "1" + "0" * 999
    assert number_text("1e-1000") == "0." + "0" * 999 + "1"
    assert number_text("123456789012345678901234567890.0100") == "123456789012345678901234567890.01"
    assert number_text("-0e99999999") == "0"


def test_new_time_keys_and_relation_matching_use_the_same_instant():
    definition = {"key_semantics": "canonical", "properties": [{"key": "at", "data_type": "datetime"}]}
    left, right = "2026-11-10T10:00:00Z", "2026-11-10T18:00:00+08:00"
    assert key_values(definition, {"at": left}, ["at"]) == key_values(definition, {"at": right}, ["at"])
    family = _family(_property(definition, "at", "owner"))
    assert _value_key(left, family) == _value_key(right, family)
    legacy = {**definition, "key_semantics": "legacy"}
    assert key_values(legacy, {"at": left}, ["at"]) != key_values(legacy, {"at": right}, ["at"])


def test_temporal_key_upgrade_previews_existing_collisions_without_rewriting_release(platform):
    client, app, owner, _ = platform
    model = {
        "types": [
            {
                "key": "Event",
                "label": "Event",
                "key_semantics": "legacy",
                "properties": [{"key": "at", "label": "Time", "data_type": "datetime"}],
                "unique_keys": [["at"]],
            }
        ]
    }
    release = publish(client, owner["project_id"], model)
    for at in ("2026-11-10T10:00:00Z", "2026-11-10T18:00:00+08:00"):
        assert (
            client.post(
                "/api/objects",
                json={"project_id": owner["project_id"], "type_key": "Event", "properties": {"at": at}},
            ).status_code
            == 201
        )
    model["types"][0]["key_semantics"] = "canonical"
    change = client.post(
        "/api/changesets",
        json={
            "project_id": owner["project_id"],
            "title": "Canonical time",
            "definition": model,
            "expected_baseline_release_id": release["release_id"],
        },
    ).json()
    validated = client.post(
        f"/api/changesets/{change['id']}/validate", json={"expected_revision": change["revision"]}
    ).json()
    assert validated["validation"]["valid"] is False
    assert validated["validation"]["error_count"] == 1
    current = client.get("/api/ontology/definition", params={"project_id": owner["project_id"]}).json()
    assert current["release_id"] == release["release_id"]
    assert current["definition"]["types"][0]["key_semantics"] == "legacy"


def test_canonical_datetime_keeps_microseconds_and_rejects_silent_precision_loss():
    from ontology.core.scalars import canonical_time
    from ontology.core.domain import validate_properties
    from ontology.core.errors import DomainError

    assert canonical_time("datetime", "0001-01-01T00:00:00+01:00") == "-3600000000"
    assert canonical_time("datetime", "2026-01-01T00:00:00.000001Z") != canonical_time(
        "datetime", "2026-01-01T00:00:00.000002Z"
    )
    definition = {"key_semantics": "canonical", "properties": [{"key": "at", "data_type": "datetime"}]}
    with pytest.raises(DomainError) as error:
        validate_properties(definition, {"at": "2026-01-01T00:00:00.0000001Z"})
    assert error.value.status == 422


def test_editing_legacy_type_does_not_implicitly_upgrade_key_semantics(platform):
    client, _, owner, _ = platform
    definition = {"types": [{"key": "Old", "label": "Old", "key_semantics": "legacy", "properties": []}]}
    release = publish(client, owner["project_id"], definition)
    del definition["types"][0]["key_semantics"]
    definition["types"].append({"key": "New", "label": "New", "properties": []})
    change = client.post(
        "/api/changesets",
        json={
            "project_id": owner["project_id"],
            "title": "Labels only",
            "expected_baseline_release_id": release["release_id"],
            "definition": definition,
        },
    ).json()
    assert [t["key_semantics"] for t in change["definition"]["types"]] == ["legacy", "canonical"]
    edited = client.patch(
        f"/api/changesets/{change['id']}",
        json={"expected_revision": change["revision"], "definition": definition},
    )
    assert edited.status_code == 200, edited.text
    assert [t["key_semantics"] for t in edited.json()["definition"]["types"]] == ["legacy", "canonical"]
