"""Inactive materialized objects remain inspectable, but are not current business inputs."""

from copy import deepcopy

import pytest
from ontology.actions.runtime import claim_action, execute_claim
from ontology.identity.models import Membership
from ontology.objects.models import Entity
from sqlalchemy import select

from tests.support.actions import action_platform as action_platform  # noqa: PLC0414 - pytest fixture registration
from tests.support.actions import pg_action_platform as pg_action_platform  # noqa: PLC0414 - pytest fixture registration
from tests.support.actions import remote as remote  # noqa: PLC0414 - pytest fixture registration
from tests.support.actions import request_approved
from tests.support.functions import execution, published
from tests.support.functions import function_platform as function_platform  # noqa: PLC0414 - pytest fixture registration
from tests.support.functions import pg_function_platform as pg_function_platform  # noqa: PLC0414 - pytest fixture registration
from tests.support.platform import publish
from tests.support.postgres import pg_databases as pg_databases  # noqa: PLC0414 - pytest fixture registration
from tests.support.postgres import pg_platform as pg_platform  # noqa: PLC0414 - pytest fixture registration


def set_status(app, tenant_id, object_id, status, properties=None):
    with app.state.db.transaction(tenant_id) as session:
        obj = session.get(Entity, object_id)
        obj.data_status = status
        if properties is not None:
            obj.properties = properties


@pytest.mark.parametrize("platform_name", ["platform", "pg_platform"])
def test_attention_listing_requires_source_permission_and_preserves_type_search_and_pagination(
    request, platform_name, definition
):
    client, app, a, _ = request.getfixturevalue(platform_name)
    definition = deepcopy(definition)
    definition["types"][0]["properties"].append(
        {"key": "secret", "label": "内部说明", "data_type": "string", "read_roles": ["owner"]}
    )
    definition["types"].append(
        {
            "key": "Other",
            "label": "其他",
            "properties": [{"key": "name", "label": "名称", "data_type": "string"}],
            "relations": [],
        }
    )
    publish(client, a["project_id"], definition)
    ids = {}
    for status in ("active", "conflict", "incomplete", "withdrawn"):
        obj = client.post(
            "/api/objects",
            json={
                "project_id": a["project_id"],
                "type_key": "Project",
                "properties": {"name": f"公开 {status}", "secret": "hidden-search-marker"},
            },
        ).json()
        ids[status] = obj["id"]
        set_status(app, a["tenant_id"], obj["id"], status)
    other = client.post(
        "/api/objects",
        json={"project_id": a["project_id"], "type_key": "Other", "properties": {"name": "其他类型"}},
    ).json()
    set_status(app, a["tenant_id"], other["id"], "incomplete")
    deleted = client.post(
        "/api/objects",
        json={"project_id": a["project_id"], "type_key": "Project", "properties": {"name": "已删除"}},
    ).json()
    with app.state.db.transaction(a["tenant_id"]) as session:
        row = session.get(Entity, deleted["id"])
        row.data_status, row.deleted = "withdrawn", True
    base = {"project_id": a["project_id"], "data_status": "attention", "type_key": "Project"}
    first = client.get("/api/objects", params={**base, "limit": 2}).json()
    second = client.get("/api/objects", params={**base, "limit": 2, "offset": 2}).json()
    assert first["total"] == second["total"] == 3
    assert {row["id"] for row in first["items"] + second["items"]} == {
        ids[key] for key in ("conflict", "incomplete", "withdrawn")
    }
    assert (
        client.get("/api/objects", params={"project_id": a["project_id"], "data_status": "attention"}).json()[
            "total"
        ]
        == 4
    )
    for status, object_id in ids.items():
        result = client.get("/api/objects", params={**base, "data_status": status}).json()
        assert [row["id"] for row in result["items"]] == [object_id]
    searched = client.get("/api/objects", params={**base, "q": "incomplete"}).json()
    assert searched["total"] == 1 and searched["items"][0]["id"] == ids["incomplete"]
    assert client.get("/api/objects", params={**base, "data_status": "all"}).status_code == 422
    with app.state.db.transaction(a["tenant_id"]) as session:
        member = session.scalar(
            select(Membership).where(
                Membership.project_id == a["project_id"], Membership.user_id == a["user_id"]
            )
        )
        member.role = "modeler"
    modeler = client.get("/api/objects", params=base)
    assert modeler.status_code == 200 and modeler.json()["total"] == 3
    assert all("secret" not in row["properties"] for row in modeler.json()["items"])
    assert client.get("/api/objects", params={**base, "q": "hidden-search-marker"}).json()["total"] == 0
    with app.state.db.transaction(a["tenant_id"]) as session:
        session.scalar(
            select(Membership).where(
                Membership.project_id == a["project_id"], Membership.user_id == a["user_id"]
            )
        ).role = "operator"
    for status in ("attention", "conflict", "incomplete", "withdrawn"):
        rejected = client.get("/api/objects", params={**base, "data_status": status})
        assert rejected.status_code == 403 and "items" not in rejected.json()
    current = client.get("/api/objects", params={"project_id": a["project_id"]})
    assert current.status_code == 200 and [row["id"] for row in current.json()["items"]] == [ids["active"]]


@pytest.mark.parametrize("platform_name", ["platform", "pg_platform"])
@pytest.mark.parametrize("status", ["conflict", "incomplete", "withdrawn"])
def test_inactive_objects_hidden_from_default_queries_graph_relations_but_detail_is_readonly(
    request, platform_name, definition, status
):
    client, app, a, _ = request.getfixturevalue(platform_name)
    publish(client, a["project_id"], definition)
    objects = [
        client.post(
            "/api/objects",
            json={
                "project_id": a["project_id"],
                "type_key": "Project",
                "properties": {"name": name, "status": "open"},
            },
        ).json()
        for name in ("有效对象", "待修复对象")
    ]
    source, target = objects
    relation = {
        "project_id": a["project_id"],
        "type_key": "parent",
        "source_id": source["id"],
        "target_id": target["id"],
    }
    assert client.post("/api/relations", json=relation).status_code == 201
    # Mirrors the contribution materializer: conflicting/withdrawn source value is absent,
    # while an independent manual value is retained. Revision is intentionally unchanged
    # to prove the new guard, rather than accidentally relying on an existing CAS guard.
    set_status(app, a["tenant_id"], target["id"], status, {"status": "open"})
    for params in (
        {"project_id": a["project_id"]},
        {"project_id": a["project_id"], "type_key": "Project"},
        {"project_id": a["project_id"], "q": "open"},
    ):
        response = client.get("/api/objects", params=params)
        assert response.status_code == 200, response.text
        assert [row["id"] for row in response.json()["items"]] == [source["id"]]
        assert response.json()["total"] == 1
    queried = client.post(
        "/api/query",
        json={
            "project_id": a["project_id"],
            "type_key": "Project",
            "filters": [{"field": "status", "op": "eq", "value": "open"}],
        },
    )
    assert queried.status_code == 200, queried.text
    assert queried.json()["total"] == 1
    graph = client.get("/api/graph", params={"project_id": a["project_id"], "limit": 1}).json()
    assert [node["id"] for node in graph["nodes"]] == [source["id"]]
    assert graph["edges"] == [] and not graph["truncated"]
    relations = client.get("/api/relations", params={"project_id": a["project_id"]}).json()
    assert relations == {"items": [], "total": 0}
    detail = client.get(f"/api/objects/{target['id']}")
    assert detail.status_code == 200 and detail.json()["data_status"] == status
    assert detail.json()["properties"] == {"status": "open"}
    patch = client.patch(
        f"/api/objects/{target['id']}",
        json={"expected_revision": target["revision"], "properties": {"name": "不能伪装修复"}},
    )
    assert patch.status_code == 409 and patch.json()["error"]["code"] == "object_not_active"
    for values in (relation, {**relation, "source_id": target["id"], "target_id": source["id"]}):
        response = client.post("/api/relations", json=values)
        assert response.status_code == 409 and response.json()["error"]["code"] == "object_not_active"
    assert client.get(f"/api/objects/{target['id']}").json()["properties"] == {"status": "open"}

    # Missing required fields in historical inactive materialization do not block
    # an unrelated label-only release; deleting its type must still be rejected.
    changed = deepcopy(definition)
    changed["types"][0]["label"] = "项目（说明更新）"
    publish(client, a["project_id"], changed)
    baseline = client.get("/api/ontology/definition", params={"project_id": a["project_id"]}).json()[
        "release_id"
    ]
    draft = client.post(
        "/api/changesets",
        json={
            "project_id": a["project_id"],
            "title": "不能丢失历史对象类型",
            "definition": {"types": []},
            "expected_baseline_release_id": baseline,
        },
    ).json()
    validation = client.post(
        f"/api/changesets/{draft['id']}/validate", json={"expected_revision": draft["revision"]}
    ).json()["validation"]
    assert not validation["valid"]
    assert any(error.get("object_id") == target["id"] for error in validation["errors"])


@pytest.mark.parametrize("status", ["conflict", "incomplete", "withdrawn"])
@pytest.mark.parametrize("platform_name", ["action_platform", "pg_action_platform"])
def test_action_preview_enqueue_and_worker_recheck_inactive_target(request, platform_name, status):
    p = request.getfixturevalue(platform_name)
    client, app, a, _, payload, remote_state = p
    set_status(app, a["tenant_id"], payload["target_id"], status)
    preview = client.post(
        "/api/actions/preview",
        json={key: value for key, value in payload.items() if key != "idempotency_key"},
    )
    assert preview.status_code == 409 and preview.json()["error"]["code"] == "object_not_active"
    assert client.post("/api/actions", json=payload).status_code == 409
    set_status(app, a["tenant_id"], payload["target_id"], "active")
    approved = request_approved(p)
    set_status(app, a["tenant_id"], payload["target_id"], status)
    assert (
        client.post(
            f"/api/actions/{approved['id']}/execute", json={"expected_revision": approved["revision"]}
        ).status_code
        == 409
    )
    set_status(app, a["tenant_id"], payload["target_id"], "active")
    queued = client.post(
        f"/api/actions/{approved['id']}/execute", json={"expected_revision": approved["revision"]}
    )
    assert queued.status_code == 200, queued.text
    claim = claim_action(app.state.db, a["tenant_id"], "worker-status-test")
    assert claim
    set_status(app, a["tenant_id"], payload["target_id"], status)
    assert execute_claim(app.state.db, claim, app.state.action_adapters) == "blocked"
    assert remote_state["post_calls"] == 0


@pytest.mark.parametrize("status", ["conflict", "incomplete", "withdrawn"])
@pytest.mark.parametrize("platform_name", ["function_platform", "pg_function_platform"])
def test_function_run_and_history_reauthorize_current_input_status_without_changing_kernel(
    request, platform_name, status
):
    p = request.getfixturevalue(platform_name)
    client, app, a = p[:3]
    version = published(p)
    body = execution(p, version)
    response = client.post(f"/api/functions/{version['id']}/runs", json=body)
    assert response.status_code == 201, response.text
    run = response.json()
    set_status(app, a["tenant_id"], p[5]["id"], status)
    blocked = client.post(f"/api/functions/{version['id']}/runs", json=body)
    assert blocked.status_code == 409 and blocked.json()["error"]["code"] == "object_not_active"
    assert client.get(f"/api/function-runs/{run['id']}").status_code == 403
    history = client.get(f"/api/functions/{version['id']}/runs", params={"limit": 1, "offset": 0})
    assert history.status_code == 200 and history.json() == {"items": [], "next_offset": None}
    set_status(app, a["tenant_id"], p[5]["id"], "active")
    assert client.get(f"/api/function-runs/{run['id']}").status_code == 200
