import pytest
from ontology.objects.models import Entity, EntityKey
from ontology.worker import process_one
from sqlalchemy import select

from tests.support.platform import publish
from tests.support.postgres import pg_databases as pg_databases  # noqa: PLC0414 - pytest fixture registration
from tests.support.postgres import pg_platform as pg_platform  # noqa: PLC0414 - pytest fixture registration


@pytest.mark.parametrize("fixture_name", ["platform", "pg_platform"])
def test_numeric_business_keys_never_round_long_decimals(request, fixture_name):
    client, _app, owner, _ = request.getfixturevalue(fixture_name)
    model = {
        "types": [
            {
                "key": "Exact",
                "label": "Exact key",
                "properties": [{"key": "code", "label": "Exact decimal", "data_type": "number"}],
                "unique_keys": [["code"]],
            }
        ]
    }
    publish(client, owner["project_id"], model)

    def create(value):
        return client.post(
            "/api/objects",
            json={"project_id": owner["project_id"], "type_key": "Exact", "properties": {"code": value}},
        )

    assert create("123456789012345678901234567890.01").status_code == 201
    assert create("123456789012345678901234567890.02").status_code == 201
    assert create("123456789012345678901234567890.0100").status_code == 409
    assert create("-0.00").status_code == 201
    assert create("0").status_code == 409


@pytest.mark.parametrize("fixture_name", ["platform", "pg_platform"])
def test_composite_key_conflicts_and_delta_review(request, fixture_name, definition):
    client, app, owner, _ = request.getfixturevalue(fixture_name)
    definition["types"][0]["unique_keys"] = [["name", "status"]]
    release = publish(client, owner["project_id"], definition)
    data = {
        "project_id": owner["project_id"],
        "type_key": "Project",
        "properties": {"name": "A", "status": "open"},
    }
    a = client.post("/api/objects", json=data)
    assert a.status_code == 201, a.text
    assert client.post("/api/objects", json=data).status_code == 409
    b = client.post("/api/objects", json={**data, "properties": {"name": "B", "status": "open"}}).json()
    assert (
        client.patch(
            "/api/objects/" + b["id"], json={"expected_revision": b["revision"], "properties": {"name": "A"}}
        ).status_code
        == 409
    )
    with app.state.db.transaction(owner["tenant_id"]) as session:
        assert len(session.scalars(select(EntityKey)).all()) == 2
    # Changing a previously valid object after validation must be rechecked.
    next_definition = {
        "types": [
            {
                **definition["types"][0],
                "properties": [
                    {**p, "enum": ["open"]} if p["key"] == "status" else p
                    for p in definition["types"][0]["properties"]
                ],
            }
        ]
    }
    change = client.post(
        "/api/changesets",
        json={
            "project_id": owner["project_id"],
            "title": "Narrow status",
            "definition": next_definition,
            "expected_baseline_release_id": release["release_id"],
        },
    ).json()
    change = client.post(
        f"/api/changesets/{change['id']}/validate", json={"expected_revision": change["revision"]}
    ).json()
    assert (
        client.patch(
            "/api/objects/" + b["id"],
            json={"expected_revision": b["revision"], "properties": {"status": "done"}},
        ).status_code
        == 200
    )
    result = client.post(
        f"/api/changesets/{change['id']}/approve", json={"expected_revision": change["revision"]}
    )
    assert result.status_code == 422, result.text


@pytest.mark.parametrize("fixture_name", ["platform", "pg_platform"])
def test_large_validation_uses_resumable_jobs_and_checks_live_delta(
    request, fixture_name, definition, monkeypatch
):
    client, app, owner, _ = request.getfixturevalue(fixture_name)
    release = publish(client, owner["project_id"], definition)
    with app.state.db.transaction(owner["tenant_id"]) as session:
        session.add_all(
            Entity(
                tenant_id=owner["tenant_id"],
                project_id=owner["project_id"],
                type_key="Project",
                release_id=release["release_id"],
                properties={"name": f"P{i}"},
            )
            for i in range(1001)
        )
    definition["types"][0]["unique_keys"] = [["name"]]
    change = client.post(
        "/api/changesets",
        json={
            "project_id": owner["project_id"],
            "title": "Unique names",
            "definition": definition,
            "expected_baseline_release_id": release["release_id"],
        },
    ).json()
    change = client.post(
        f"/api/changesets/{change['id']}/validate", json={"expected_revision": change["revision"]}
    ).json()
    assert change["status"] == "validating" and change["validation"]["job_id"]
    # A new row can arrive between validation batches; it is not omitted.
    for _ in range(2):
        assert process_one(app.state.db, app.state.settings)
    from ontology.modeling import validation

    run = validation.run_validation

    def crash(*_):
        raise RuntimeError("Synthetic worker crash")

    monkeypatch.setattr(validation, "run_validation", crash)
    assert process_one(app.state.db, app.state.settings)
    failed = client.get(f"/api/changesets/{change['id']}").json()
    assert failed["validation_job"]["status"] == "failed"
    assert failed["validation_job"]["objects_checked"] == 500
    monkeypatch.setattr(validation, "run_validation", run)
    assert client.post(f"/api/jobs/{failed['validation_job']['id']}/retry").status_code == 200
    created = client.post(
        "/api/objects",
        json={"project_id": owner["project_id"], "type_key": "Project", "properties": {"name": "P-new"}},
    )
    assert created.status_code == 201, created.text
    for _ in range(10):
        if not process_one(app.state.db, app.state.settings):
            break
    change = client.get(f"/api/changesets/{change['id']}").json()
    assert change["status"] == "validated", change
    for operation in ("approve", "publish"):
        result = client.post(
            f"/api/changesets/{change['id']}/{operation}", json={"expected_revision": change["revision"]}
        )
        assert result.status_code == 200, result.text
        change = result.json()
    assert (
        client.post(
            "/api/objects",
            json={"project_id": owner["project_id"], "type_key": "Project", "properties": {"name": "P-new"}},
        ).status_code
        == 409
    )
