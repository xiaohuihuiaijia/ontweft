"""Trusted Python kernels and real service authorization, not a user-code sandbox."""

from copy import deepcopy
from decimal import Decimal

import pytest
from ontology.functions.models import FunctionRun, FunctionVersion
from ontology.functions.runtime import InventoryInput, inventory_gap
from ontology.objects.models import Entity
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError

from tests.support.functions import as_reviewer, execution, published, register, reviewed
from tests.support.functions import function_platform as function_platform  # noqa: PLC0414 - pytest fixture registration
from tests.support.functions import pg_function_platform as pg_function_platform  # noqa: PLC0414 - pytest fixture registration
from tests.support.platform import publish
from tests.support.postgres import pg_databases as pg_databases  # noqa: PLC0414 - pytest fixture registration
from tests.support.postgres import pg_platform as pg_platform  # noqa: PLC0414 - pytest fixture registration


def test_inventory_function_typed_exact_math_and_rejects_unbounded_input():
    assert inventory_gap(InventoryInput(demand="0.3", inventory="0.3", reserved="0.2")).model_dump(
        mode="json"
    ) == {"available": "0.1", "gap": "0.2"}
    for value in (None, True, 0.1, "NaN", "Infinity", "-1", "1e999999", "0.0000000001", "9" * 65):
        with pytest.raises(ValueError):
            InventoryInput(demand=value, inventory=1, reserved=0)
    assert inventory_gap(InventoryInput(demand=4, inventory=1, reserved=2)).gap == Decimal(4)


@pytest.mark.parametrize("fixture_name", ["function_platform", "pg_function_platform"])
def test_published_function_returns_traceable_gap_without_business_writes(request, fixture_name):
    p = request.getfixturevalue(fixture_name)
    client, app, a, _b, _data, obj, *_ = p
    version = published(p)
    before = client.get(f"/api/objects/{obj['id']}").json()
    response = client.post(f"/api/functions/{version['id']}/runs", json=execution(p, version))
    assert response.status_code == 201, response.text
    run = response.json()
    assert run["result"]["values"] == {"available": "60", "gap": "40"}
    assert run["result"]["unit"] == "件" and run["result"]["kind"] == "computed"
    assert (
        run["inputs"]["inventory"]["object_id"] == obj["id"] and run["inputs"]["inventory"]["value"] == "80"
    )
    assert run["model_release_id"] == version["baseline_release_id"]
    assert client.get(f"/api/objects/{obj['id']}").json() == before
    again = client.post(f"/api/functions/{version['id']}/runs", json=execution(p, version)).json()
    assert again["request_hash"] == run["request_hash"] and again["result"] == run["result"]
    assert client.get(f"/api/function-runs/{run['id']}").json() == run
    history = client.get(f"/api/functions/{version['id']}/runs", params={"limit": 1}).json()
    assert len(history["items"]) == 1 and "total" not in history
    assert history["next_offset"] == 1
    last = client.get(f"/api/functions/{version['id']}/runs", params={"limit": 1, "offset": 1}).json()
    assert len(last["items"]) == 1 and last["next_offset"] is None
    with app.state.db.transaction(a["tenant_id"]) as session:
        assert len(session.scalars(select(FunctionRun)).all()) == 2


def test_registration_rejects_code_dynamic_dependencies_and_mismatched_units(function_platform):
    client, _app, a, _b, data, *_ = function_platform
    catalog = client.get("/api/functions/catalog", params={"project_id": a["project_id"]}).json()
    assert catalog["user_code_execution"] is False
    assert catalog["items"][0]["self_tests"]["passed"] == 3
    for extra in (
        {"source": "import os; os.system('anything')"},
        {"dependencies": ["unapproved-package"]},
        {"kernel_key": "exec"},
        {"url": "https://example.invalid"},
    ):
        assert client.post("/api/functions", json={**data, **extra}).status_code == 422
    assert client.post("/api/functions", json={**data, "unit": "kg"}).status_code == 422


def test_self_review_and_draft_execution_are_denied_and_edit_invalidates_review(function_platform):
    p, client = function_platform, function_platform[0]
    draft = register(p)
    assert (
        client.post(f"/api/functions/{draft['id']}/review", json={"expected_revision": 1}).status_code == 403
    )
    assert client.post(f"/api/functions/{draft['id']}/runs", json=execution(p, draft)).status_code == 409
    with as_reviewer(p) as reviewer:
        review = reviewer.post(f"/api/functions/{draft['id']}/review", json={"expected_revision": 1}).json()
    edit = client.patch(
        f"/api/functions/{draft['id']}",
        json={**p[4], "expected_revision": review["revision"], "label": "复核口径"},
    )
    assert edit.status_code == 200, edit.text
    changed = edit.json()
    assert changed["status"] == "draft" and changed["reviewed_hash"] is None
    assert (
        client.post(
            f"/api/functions/{draft['id']}/publish", json={"expected_revision": changed["revision"]}
        ).status_code
        == 409
    )
    assert (
        client.patch(
            f"/api/functions/{draft['id']}", json={**p[4], "expected_revision": review["revision"]}
        ).status_code
        == 409
    )


def test_reviewer_revocation_blocks_publication(function_platform):
    p, client = function_platform, function_platform[0]
    version = reviewed(p)
    assert (
        client.post(
            "/api/members", json={"project_id": p[2]["project_id"], "user_id": p[6]["id"], "role": "member"}
        ).status_code
        == 200
    )
    assert (
        client.post(
            f"/api/functions/{version['id']}/publish", json={"expected_revision": version["revision"]}
        ).status_code
        == 409
    )


def test_object_revision_model_revision_and_immutable_publication(function_platform):
    p, client = function_platform, function_platform[0]
    version = published(p)
    assert (
        client.patch(
            f"/api/functions/{version['id']}", json={**p[4], "expected_revision": version["revision"]}
        ).status_code
        == 409
    )
    assert (
        client.patch(
            f"/api/objects/{p[5]['id']}", json={"expected_revision": 1, "properties": {"inventory": "90"}}
        ).status_code
        == 200
    )
    assert client.post(f"/api/functions/{version['id']}/runs", json=execution(p, version)).status_code == 409
    publish(client, p[2]["project_id"], p[7])
    changed_input = execution(p, version)
    for item in changed_input["objects"].values():
        item["expected_revision"] = 2
    assert client.post(f"/api/functions/{version['id']}/runs", json=changed_input).status_code == 409


def test_current_field_permission_protects_execution_and_old_results(function_platform):
    p, client = function_platform, function_platform[0]
    version = published(p)
    run = client.post(f"/api/functions/{version['id']}/runs", json=execution(p, version)).json()
    with as_reviewer(p) as reader:
        assert reader.get(f"/api/function-runs/{run['id']}").status_code == 200
        new_model = deepcopy(p[7])
        new_model["types"][0]["properties"][1]["read_roles"] = ["owner"]
        publish(client, p[2]["project_id"], new_model)
        assert reader.get(f"/api/function-runs/{run['id']}").status_code == 403
        assert reader.get(f"/api/functions/{version['id']}/runs").status_code == 403
        hidden = reader.get("/api/functions", params={"project_id": p[2]["project_id"], "limit": 1}).json()
        assert hidden == {"items": [], "next_offset": None}
    assert client.get(f"/api/function-runs/{run['id']}").status_code == 200


def test_source_evidence_is_clipped_to_each_input_field(function_platform):
    p, client, app = function_platform, function_platform[0], function_platform[1]
    with app.state.db.transaction(p[2]["tenant_id"]) as session:
        obj = session.get(Entity, p[5]["id"])
        obj.evidence = [
            {
                "source_record_id": "source-row",
                "fields": ["demand", "inventory", "secret"],
                "content_hash": "hidden-row-hash",
            }
        ]
    version = published(p)
    response = client.post(f"/api/functions/{version['id']}/runs", json=execution(p, version))
    assert response.status_code == 201, response.text
    inputs = response.json()["inputs"]
    assert inputs["inventory"]["evidence"] == [{"source_record_id": "source-row", "fields": ["inventory"]}]
    assert inputs["reserved"]["evidence"] == []


def test_missing_values_and_cross_project_inputs_fail_without_runs(function_platform):
    p, client, app = function_platform, function_platform[0], function_platform[1]
    version = published(p)
    bad = execution(p, version)
    bad["objects"]["demand"]["object_id"] = "unavailable-object"
    assert client.post(f"/api/functions/{version['id']}/runs", json=bad).status_code == 404
    other = client.post("/api/projects", json={"name": "另一个项目"}).json()
    publish(client, other["id"], p[7])
    foreign = client.post(
        "/api/objects",
        json={
            "project_id": other["id"],
            "type_key": "Stock",
            "properties": {"demand": "100", "inventory": "80", "reserved": "20"},
        },
    ).json()
    bad["objects"]["demand"]["object_id"] = foreign["id"]
    assert client.post(f"/api/functions/{version['id']}/runs", json=bad).status_code == 404
    assert (
        client.patch(
            f"/api/objects/{p[5]['id']}", json={"expected_revision": 1, "properties": {"reserved": None}}
        ).status_code
        == 200
    )
    current = execution(p, version)
    for item in current["objects"].values():
        item["expected_revision"] = 2
    assert client.post(f"/api/functions/{version['id']}/runs", json=current).status_code == 422
    with app.state.db.transaction(p[2]["tenant_id"]) as session:
        assert session.scalar(select(FunctionRun.id)) is None


def test_distinct_objects_bind_explicitly_and_deleted_input_revokes_history(function_platform):
    p, client, app = function_platform, function_platform[0], function_platform[1]
    version = published(p)
    data = execution(p, version)
    for name, value in (("demand", "100"), ("inventory", "80"), ("reserved", "20")):
        obj = client.post(
            "/api/objects",
            json={"project_id": p[2]["project_id"], "type_key": "Stock", "properties": {name: value}},
        ).json()
        data["objects"][name] = {"object_id": obj["id"], "expected_revision": obj["revision"]}
    response = client.post(f"/api/functions/{version['id']}/runs", json=data)
    assert response.status_code == 201, response.text
    run = response.json()
    assert run["result"]["values"] == {"available": "60", "gap": "40"}
    assert len({item["object_id"] for item in run["inputs"].values()}) == 3
    with app.state.db.transaction(p[2]["tenant_id"]) as session:
        session.get(Entity, data["objects"]["demand"]["object_id"]).deleted = True
    assert client.get(f"/api/function-runs/{run['id']}").status_code == 404
    assert client.get(f"/api/functions/{version['id']}/runs", params={"limit": 1}).json() == {
        "items": [],
        "next_offset": None,
    }


def test_postgres_function_runs_immutable_and_tenant_scoped(pg_function_platform):
    p, client, app = pg_function_platform, pg_function_platform[0], pg_function_platform[1]
    version = published(p)
    response = client.post(f"/api/functions/{version['id']}/runs", json=execution(p, version))
    assert response.status_code == 201, response.text
    run = response.json()
    with app.state.db.transaction(p[3]["tenant_id"]) as session:
        assert session.get(FunctionVersion, version["id"]) is None
        assert session.get(FunctionRun, run["id"]) is None
    with pytest.raises(DBAPIError), app.state.db.transaction(p[2]["tenant_id"]) as session:
        session.execute(text("UPDATE function_runs SET result = '{}' WHERE id = :id"), {"id": run["id"]})
    with pytest.raises(DBAPIError), app.state.db.transaction(p[2]["tenant_id"]) as session:
        session.execute(text("DELETE FROM function_runs WHERE id = :id"), {"id": run["id"]})
