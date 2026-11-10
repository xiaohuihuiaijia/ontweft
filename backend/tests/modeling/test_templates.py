import pytest
from fastapi.testclient import TestClient
from ontology.core.domain import definition_errors
from ontology.modeling.templates import starter_templates

from tests.support.platform import publish
from tests.support.postgres import pg_databases as pg_databases  # noqa: PLC0414
from tests.support.postgres import pg_platform as pg_platform  # noqa: PLC0414


def test_templates_are_fresh_valid_definitions():
    first = starter_templates()
    assert {item["id"] for item in first} == {"project_delivery", "asset_locations"}
    assert all(not definition_errors(item["definition"]) for item in first)
    first[0]["definition"]["types"].clear()
    assert len(starter_templates()[0]["definition"]["types"]) == 2


@pytest.mark.parametrize("fixture_name", ["platform", "pg_platform"])
def test_template_read_is_authorized_and_has_no_write_side_effects(request, fixture_name):
    client, app, owner, other = request.getfixturevalue(fixture_name)
    params = {"project_id": owner["project_id"]}
    response = client.get("/api/ontology/templates", params=params)
    assert response.status_code == 200
    assert response.json()["total"] == 2
    assert client.get("/api/changesets", params=params).json()["total"] == 0
    assert client.get("/api/releases", params=params).json()["total"] == 0
    assert (
        client.get("/api/ontology/templates", params={"project_id": other["project_id"]}).status_code == 404
    )
    with TestClient(app) as anonymous:
        assert anonymous.get("/api/ontology/templates", params=params).status_code == 401


def test_read_only_member_cannot_load_management_templates(platform):
    client, app, owner, _ = platform
    created = client.post(
        "/api/users",
        json={
            "email": "reader@example.test",
            "display_name": "Reader",
            "password": "Only-Test-Password-2026",
        },
    )
    assert created.status_code == 201
    assert (
        client.post(
            "/api/members",
            json={"project_id": owner["project_id"], "user_id": created.json()["id"], "role": "member"},
        ).status_code
        == 200
    )
    with TestClient(app) as reader:
        assert (
            reader.post(
                "/api/auth/login",
                json={
                    "tenant_slug": "alpha",
                    "email": "reader@example.test",
                    "password": "Only-Test-Password-2026",
                },
            ).status_code
            == 200
        )
        assert (
            reader.get("/api/ontology/templates", params={"project_id": owner["project_id"]}).status_code
            == 403
        )


@pytest.mark.parametrize("fixture_name", ["platform", "pg_platform"])
@pytest.mark.parametrize(
    "template_id,target_type,source_type,properties,relation",
    [
        (
            "project_delivery",
            "project",
            "task",
            {"name": "首个任务", "status": "待处理", "due_date": "2026-11-20"},
            "belongs_to",
        ),
        (
            "asset_locations",
            "location",
            "asset",
            {"name": "示例资产", "code": "SYNTHETIC-001", "status": "在用"},
            "located_at",
        ),
    ],
)
def test_template_can_be_customized_published_and_used_for_typed_objects(
    request, fixture_name, template_id, target_type, source_type, properties, relation
):
    client, _, owner, _ = request.getfixturevalue(fixture_name)
    project_id = owner["project_id"]
    templates = client.get("/api/ontology/templates", params={"project_id": project_id}).json()["items"]
    definition = next(item["definition"] for item in templates if item["id"] == template_id)
    # A user changes the model before publication; no special template write path exists.
    definition["types"][0]["label"] = "我的业务分类"
    publish(client, project_id, definition)
    published = client.get("/api/ontology/definition", params={"project_id": project_id}).json()
    baseline = published["release_id"]
    assert baseline and published["definition"]["types"][0]["label"] == "我的业务分类"
    common = {"project_id": project_id, "expected_model_release_id": baseline}
    target = client.post(
        "/api/objects", json={**common, "type_key": target_type, "properties": {"name": "合成示例"}}
    )
    assert target.status_code == 201, target.text
    source = client.post("/api/objects", json={**common, "type_key": source_type, "properties": properties})
    assert source.status_code == 201, source.text
    link = client.post(
        "/api/relations",
        json={
            **common,
            "type_key": relation,
            "source_id": source.json()["id"],
            "target_id": target.json()["id"],
        },
    )
    assert link.status_code == 201, link.text
    invalid = client.post(
        "/api/objects",
        json={**common, "type_key": source_type, "properties": {**properties, "status": "未定义的选项"}},
    )
    assert invalid.status_code == 422
    wrong_target = client.post(
        "/api/relations",
        json={
            **common,
            "type_key": relation,
            "source_id": source.json()["id"],
            "target_id": source.json()["id"],
        },
    )
    assert wrong_target.status_code == 422
