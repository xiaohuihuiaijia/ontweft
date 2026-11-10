"""Trusted Python kernels and real service authorization, not a user-code sandbox."""

import pytest
from fastapi.testclient import TestClient
from ontology.functions.api import router

from tests.support.platform import publish
from tests.support.postgres import pg_databases as pg_databases  # noqa: PLC0414 - pytest fixture registration
from tests.support.postgres import pg_platform as pg_platform  # noqa: PLC0414 - pytest fixture registration


def configure(platform):
    client, app, a, b = platform
    if "/api/functions/catalog" not in app.openapi()["paths"]:
        app.include_router(router, prefix="/api")
        app.openapi_schema = None
    app.state.db.create_schema()
    definition = {
        "types": [
            {
                "key": "Stock",
                "label": "库存",
                "properties": [
                    {"key": "demand", "label": "需求", "data_type": "number", "unit": "件"},
                    {"key": "inventory", "label": "库存", "data_type": "number", "unit": "件"},
                    {"key": "reserved", "label": "占用", "data_type": "number", "unit": "件"},
                ],
            }
        ]
    }
    publish(client, a["project_id"], definition)
    baseline = client.get("/api/ontology/definition", params={"project_id": a["project_id"]}).json()[
        "release_id"
    ]
    obj_response = client.post(
        "/api/objects",
        json={
            "project_id": a["project_id"],
            "type_key": "Stock",
            "properties": {"demand": "100", "inventory": "80", "reserved": "20"},
        },
    )
    assert obj_response.status_code == 201, obj_response.text
    obj = obj_response.json()
    reviewer = client.post(
        "/api/users",
        json={
            "email": "function-reviewer@example.test",
            "display_name": "函数审阅员",
            "password": "Function-Reviewer-Password",
        },
    ).json()
    assert (
        client.post(
            "/api/members",
            json={"project_id": a["project_id"], "user_id": reviewer["id"], "role": "reviewer"},
        ).status_code
        == 200
    )
    data = {
        "project_id": a["project_id"],
        "key": "stock_gap",
        "label": "库存缺口",
        "baseline_release_id": baseline,
        "unit": "件",
        "bindings": {
            name: {"type_key": "Stock", "field": name} for name in ("demand", "inventory", "reserved")
        },
    }
    return client, app, a, b, data, obj, reviewer, definition


@pytest.fixture
def function_platform(platform):
    return configure(platform)


@pytest.fixture
def pg_function_platform(request):
    return configure(request.getfixturevalue("pg_platform"))


def as_reviewer(p):
    client, app = p[:2]
    reviewer = TestClient(app)
    slug = client.get("/api/auth/me").json()["tenant"]["slug"]
    response = reviewer.post(
        "/api/auth/login",
        json={
            "tenant_slug": slug,
            "email": "function-reviewer@example.test",
            "password": "Function-Reviewer-Password",
        },
    )
    assert response.status_code == 200, response.text
    return reviewer


def register(p):
    response = p[0].post("/api/functions", json=p[4])
    assert response.status_code == 201, response.text
    return response.json()


def reviewed(p):
    version = register(p)
    with as_reviewer(p) as reviewer:
        response = reviewer.post(
            f"/api/functions/{version['id']}/review", json={"expected_revision": version["revision"]}
        )
        assert response.status_code == 200, response.text
    return response.json()


def published(p):
    version = reviewed(p)
    response = p[0].post(
        f"/api/functions/{version['id']}/publish", json={"expected_revision": version["revision"]}
    )
    assert response.status_code == 200, response.text
    return response.json()


def execution(p, version):
    return {
        "expected_function_revision": version["revision"],
        "objects": {
            name: {"object_id": p[5]["id"], "expected_revision": p[5]["revision"]}
            for name in ("demand", "inventory", "reserved")
        },
    }
