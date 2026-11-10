from fastapi.testclient import TestClient

from tests.support.platform import publish


def test_published_model_drives_object_validation_and_cas(platform, definition):
    client, _app, a, _ = platform
    publish(client, a["project_id"], definition)
    response = client.post(
        "/api/objects",
        json={
            "project_id": a["project_id"],
            "type_key": "Project",
            "properties": {"name": "知识项目", "budget": "123.456", "status": "open"},
        },
    )
    assert response.status_code == 201, response.text
    obj = response.json()
    assert obj["properties"]["budget"] == "123.456"
    edit = client.patch(
        f"/api/objects/{obj['id']}", json={"expected_revision": 1, "properties": {"name": "新名称"}}
    )
    assert edit.status_code == 200, edit.text
    assert edit.json()["revision"] == 2
    stale = client.patch(
        f"/api/objects/{obj['id']}", json={"expected_revision": 1, "properties": {"name": "覆盖"}}
    )
    assert stale.status_code == 409
    invalid = client.patch(
        f"/api/objects/{obj['id']}", json={"expected_revision": 2, "properties": {"status": "missing"}}
    )
    assert invalid.status_code == 422
    history = client.get(f"/api/objects/{obj['id']}/history").json()
    assert [x["revision"] for x in history["items"]] == [2, 1]
    assert client.get(f"/api/objects/{obj['id']}").json()["properties"]["name"] == "新名称"


def test_tenant_and_project_isolation(platform, definition):
    client, app, a, b = platform
    publish(client, a["project_id"], definition)
    assert client.get("/api/objects", params={"project_id": b["project_id"]}).status_code == 404
    assert (
        client.post(
            "/api/changesets", json={"project_id": b["project_id"], "title": "越权", "definition": definition}
        ).status_code
        == 404
    )
    with TestClient(app) as other:
        assert other.get("/api/projects").status_code == 401
        other.post(
            "/api/auth/login",
            json={"tenant_slug": "beta", "email": "owner@example.com", "password": "Safe-Test-Password-2026"},
        )
        assert other.get("/api/ontology/types", params={"project_id": a["project_id"]}).status_code == 404


def test_field_read_filter_sort_and_history_permissions(platform, definition):
    client, app, a, _ = platform
    publish(client, a["project_id"], definition)
    obj = client.post(
        "/api/objects",
        json={
            "project_id": a["project_id"],
            "type_key": "Project",
            "properties": {"name": "公开", "budget": "999.12"},
        },
    ).json()
    person = client.post(
        "/api/users",
        json={"email": "member@example.com", "display_name": "普通成员", "password": "Safe-Member-Password"},
    ).json()
    assert (
        client.post(
            "/api/members", json={"project_id": a["project_id"], "user_id": person["id"], "role": "member"}
        ).status_code
        == 200
    )
    with TestClient(app) as member:
        member.post(
            "/api/auth/login",
            json={"tenant_slug": "alpha", "email": "member@example.com", "password": "Safe-Member-Password"},
        )
        assert "budget" not in member.get(f"/api/objects/{obj['id']}").json()["properties"]
        assert (
            "budget" not in member.get(f"/api/objects/{obj['id']}/history").json()["items"][0]["properties"]
        )
        query = {
            "project_id": a["project_id"],
            "type_key": "Project",
            "filters": [{"field": "budget", "op": "gt", "value": "0"}],
        }
        assert member.post("/api/query", json=query).status_code == 403
        assert (
            member.post("/api/query", json={**query, "filters": [], "sort_field": "budget"}).status_code
            == 403
        )
        assert (
            member.get("/api/objects", params={"project_id": a["project_id"], "q": "999.12"}).json()["total"]
            == 0
        )
        model_graph = member.get("/api/graph", params={"project_id": a["project_id"], "kind": "model"}).json()
        assert model_graph["nodes"][0]["visible_property_count"] == 2
        assert "budget" not in str(model_graph)
        instance_graph = member.get("/api/graph", params={"project_id": a["project_id"]}).json()
        assert "budget" not in str(instance_graph) and "999.12" not in str(instance_graph)
        assert (
            member.patch(
                f"/api/objects/{obj['id']}", json={"expected_revision": 1, "properties": {"name": "改名"}}
            ).status_code
            == 403
        )
        client.post(f"/api/users/{person['id']}/disable")
        assert member.get("/api/projects").status_code == 401


def test_changeset_edit_invalidates_approval_and_stale_baseline(platform, definition):
    client, _app, a, _ = platform
    draft = client.post(
        "/api/changesets", json={"project_id": a["project_id"], "title": "A", "definition": definition}
    ).json()
    validated = client.post(f"/api/changesets/{draft['id']}/validate", json={"expected_revision": 1}).json()
    approved = client.post(
        f"/api/changesets/{draft['id']}/approve", json={"expected_revision": validated["revision"]}
    ).json()
    edited = client.patch(
        f"/api/changesets/{draft['id']}", json={"expected_revision": approved["revision"], "title": "B"}
    ).json()
    assert edited["status"] == "draft" and edited["approved_by"] is None
    assert (
        client.post(
            f"/api/changesets/{draft['id']}/publish", json={"expected_revision": edited["revision"]}
        ).status_code
        == 409
    )
    publish(client, a["project_id"], definition)
    assert (
        client.post(
            f"/api/changesets/{draft['id']}/validate", json={"expected_revision": edited["revision"]}
        ).status_code
        == 409
    )


def test_relations_validate_roles_cardinality_and_graph(platform, definition):
    client, _app, a, _ = platform
    publish(client, a["project_id"], definition)
    ids = [
        client.post(
            "/api/objects",
            json={"project_id": a["project_id"], "type_key": "Project", "properties": {"name": name}},
        ).json()["id"]
        for name in ["父", "子", "另一个"]
    ]
    payload = {"project_id": a["project_id"], "type_key": "parent", "source_id": ids[1], "target_id": ids[0]}
    assert client.post("/api/relations", json=payload).status_code == 201
    assert client.post("/api/relations", json={**payload, "target_id": ids[2]}).status_code == 409
    graph = client.get("/api/graph", params={"project_id": a["project_id"]}).json()
    assert len(graph["nodes"]) == 3 and len(graph["edges"]) == 1
    assert graph["release_id"] and graph["version"] == 1
    assert {node["type_label"] for node in graph["nodes"]} == {"项目"}
    assert graph["edges"][0]["label"] == "父项目"
    assert graph["edges"][0]["cardinality"] == "one"
    model_graph = client.get("/api/graph", params={"project_id": a["project_id"], "kind": "model"}).json()
    assert model_graph["release_id"] == graph["release_id"]
    assert model_graph["edges"][0]["cardinality"] == "one"


def test_incompatible_model_cannot_publish_over_objects(platform, definition):
    client, _app, a, _ = platform
    publish(client, a["project_id"], definition)
    client.post(
        "/api/objects",
        json={"project_id": a["project_id"], "type_key": "Project", "properties": {"name": "保留"}},
    )
    baseline = client.get("/api/ontology/definition", params={"project_id": a["project_id"]}).json()[
        "release_id"
    ]
    draft = client.post(
        "/api/changesets",
        json={
            "project_id": a["project_id"],
            "title": "删除类型",
            "definition": {"types": []},
            "expected_baseline_release_id": baseline,
        },
    ).json()
    result = client.post(f"/api/changesets/{draft['id']}/validate", json={"expected_revision": 1}).json()
    assert result["validation"]["valid"] is False
    assert result["validation"]["objects_checked"] == 1
    assert (
        client.post(
            f"/api/changesets/{draft['id']}/approve", json={"expected_revision": result["revision"]}
        ).status_code
        == 422
    )


def test_cross_site_mutations_are_rejected(platform):
    client, *_ = platform
    response = client.post(
        "/api/projects", json={"name": "危险"}, headers={"Origin": "https://attacker.invalid"}
    )
    assert response.status_code == 403


def test_draft_creation_rejects_definition_loaded_before_concurrent_publish(platform, definition):
    client, _, a, _ = platform
    baseline = client.get("/api/ontology/definition", params={"project_id": a["project_id"]}).json()[
        "release_id"
    ]
    publish(client, a["project_id"], definition)
    response = client.post(
        "/api/changesets",
        json={
            "project_id": a["project_id"],
            "title": "陈旧完整定义",
            "definition": definition,
            "expected_baseline_release_id": baseline,
        },
    )
    assert response.status_code == 409
