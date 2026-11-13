from copy import deepcopy

import pytest
from ontology.modeling.diff import definition_diff
from ontology.modeling.schemas import Definition, RelationDefinition
from ontology.objects.models import Relation
from pydantic import ValidationError
from sqlalchemy import select

from tests.support.platform import publish


def test_empty_new_fields_do_not_make_old_release_a_change(definition):
    current = Definition.model_validate(definition).model_dump()
    current["types"][0]["key_semantics"] = "legacy"
    assert definition_diff(definition, current)["items"] == []


def test_aliases_change_display_without_replacing_identity(definition):
    current = deepcopy(definition)
    current["types"][0].update(aliases=["  工程 ", "工程", ""], label="业务项目")
    current = Definition.model_validate(current).model_dump()
    current["types"][0]["key_semantics"] = "legacy"
    assert current["types"][0]["aliases"] == ["工程"]
    diff = definition_diff(definition, current)
    assert len(diff["items"]) == 1
    assert diff["items"][0]["risk"] == "display"
    assert diff["affected_type_keys"] == []


@pytest.mark.parametrize("flag", ["required", "source_owned"])
def test_relation_properties_reject_unsupported_contracts(flag):
    with pytest.raises(ValidationError, match="关系属性目前仅支持可选字段"):
        RelationDefinition.model_validate(
            {
                "key": "parent",
                "label": "父级",
                "target_type": "Project",
                "properties": [{"key": "condition", "label": "条件", "data_type": "string", flag: True}],
            }
        )


def test_diff_covers_field_relation_mapping_and_removal(definition):
    before = deepcopy(definition)
    before["mappings"] = [{"key": "products", "type_key": "Project", "source_id": "s"}]
    after = deepcopy(before)
    kind = after["types"][0]
    kind["properties"][0]["label"] = "项目名称"
    kind["properties"][1]["data_type"] = "integer"
    kind["relations"][0]["cardinality"] = "many"
    kind["relations"][0]["properties"] = [{"key": "condition", "label": "条件", "data_type": "string"}]
    diff = definition_diff(before, after)
    by_key = {(item["kind"], item["key"]): item for item in diff["items"]}
    assert by_key["property", "name"]["risk"] == "display"
    assert by_key["property", "budget"]["risk"] == "review"
    assert by_key["relation", "parent"]["risk"] == "compatible"
    assert by_key["relation_property", "parent.condition"]["risk"] == "compatible"
    assert diff["affected_mapping_keys"] == ["products"]
    removed = definition_diff(after, {"types": [], "mappings": []})
    assert removed["counts"]["removed"] == 2
    assert all(item["risk"] == "review" for item in removed["items"])


def test_relation_attribute_narrowing_blocks_release_and_reports_scanned_impact(platform, definition):
    client, app, owner, _ = platform
    field = {"key": "condition", "label": "适用条件", "data_type": "enum", "enum": ["indoor", "outdoor"]}
    definition["types"][0]["relations"][0]["properties"] = [field]
    release = publish(client, owner["project_id"], definition)
    objects = [
        client.post(
            "/api/objects",
            json={"project_id": owner["project_id"], "type_key": "Project", "properties": {"name": name}},
        ).json()
        for name in ("A", "B")
    ]
    # Seed a valid existing relation directly; this tests publication validation,
    # independently of the relation mutation API covered by its own suite.
    with app.state.db.transaction(owner["tenant_id"], write=True) as session:
        session.add(
            Relation(
                tenant_id=owner["tenant_id"],
                project_id=owner["project_id"],
                type_key="parent",
                source_id=objects[0]["id"],
                target_id=objects[1]["id"],
                properties={"condition": "outdoor"},
                evidence=[],
            )
        )
    current = deepcopy(definition)
    current["types"][0]["relations"][0]["properties"][0]["enum"] = ["indoor"]
    response = client.post(
        "/api/changesets",
        json={
            "project_id": owner["project_id"],
            "title": "收窄关系条件",
            "definition": current,
            "expected_baseline_release_id": release["release_id"],
        },
    )
    assert response.status_code == 201, response.text
    draft = response.json()
    assert "impact" not in draft["validation"]
    assert draft["model_diff"]["items"][0]["kind"] == "relation_property"
    response = client.post(
        f"/api/changesets/{draft['id']}/validate", json={"expected_revision": draft["revision"]}
    )
    assert response.status_code == 200, response.text
    report = response.json()["validation"]
    assert not report["valid"]
    assert any("关系属性" in error["message"] for error in report["errors"])
    assert report["impact"] == {
        "objects": 2,
        "relations": 1,
        "complete": True,
        "scope": "validation_scan",
        "affected_type_keys": ["Project"],
    }
    with app.state.db.transaction(owner["tenant_id"]) as session:
        assert session.scalar(select(Relation)).properties == {"condition": "outdoor"}


def test_types_response_hides_restricted_relation_definitions(platform, definition):
    client, _, owner, _ = platform
    definition["types"][0]["relations"][0]["properties"] = [
        {"key": "public", "label": "公开条件", "data_type": "string"},
        {"key": "restricted", "label": "限制条件", "data_type": "string", "read_roles": ["reviewer"]},
    ]
    publish(client, owner["project_id"], definition)
    result = client.get("/api/ontology/types", params={"project_id": owner["project_id"]}).json()
    assert [field["key"] for field in result["items"][0]["relations"][0]["properties"]] == ["public"]


def test_relation_property_edit_after_validation_is_rechecked_before_approval(platform, definition):
    client, _, owner, _ = platform
    definition["types"][0]["relations"][0]["properties"] = [
        {"key": "condition", "label": "条件", "data_type": "enum", "enum": ["indoor", "outdoor"]}
    ]
    release = publish(client, owner["project_id"], definition)
    objects = [
        client.post(
            "/api/objects",
            json={"project_id": owner["project_id"], "type_key": "Project", "properties": {"name": name}},
        ).json()
        for name in ("A", "B")
    ]
    response = client.post(
        "/api/relations",
        json={
            "project_id": owner["project_id"],
            "type_key": "parent",
            "source_id": objects[0]["id"],
            "target_id": objects[1]["id"],
            "properties": {"condition": "indoor"},
        },
    )
    assert response.status_code == 201, response.text
    relation = response.json()
    proposed = deepcopy(definition)
    proposed["types"][0]["relations"][0]["properties"][0]["enum"] = ["indoor"]
    change = client.post(
        "/api/changesets",
        json={
            "project_id": owner["project_id"],
            "title": "收窄关系条件",
            "definition": proposed,
            "expected_baseline_release_id": release["release_id"],
        },
    ).json()
    validated = client.post(
        f"/api/changesets/{change['id']}/validate", json={"expected_revision": change["revision"]}
    )
    assert validated.status_code == 200, validated.text
    change = validated.json()
    assert change["validation"]["valid"]
    response = client.patch(
        f"/api/relations/{relation['id']}",
        json={"expected_revision": relation["revision"], "properties": {"condition": "outdoor"}},
    )
    assert response.status_code == 200, response.text
    approved = client.post(
        f"/api/changesets/{change['id']}/approve", json={"expected_revision": change["revision"]}
    )
    assert approved.status_code == 422, approved.text
    assert "关系属性" in approved.text
