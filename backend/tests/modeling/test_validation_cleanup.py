import pytest
from sqlalchemy import select

from ontology.jobs.models import Job
from ontology.objects.models import EntityKey
from ontology.worker import process_one
from tests.support.platform import publish
from tests.support.postgres import pg_databases as pg_databases  # noqa: PLC0414
from tests.support.postgres import pg_platform as pg_platform  # noqa: PLC0414


@pytest.mark.parametrize("fixture_name", ["platform", "pg_platform"])
@pytest.mark.parametrize("background", [False, True])
def test_edit_discards_only_obsolete_validation_keys(
    request, fixture_name, background, definition, monkeypatch
):
    client, app, owner, _ = request.getfixturevalue(fixture_name)
    definition["types"][0]["unique_keys"] = [["name"]]
    release = publish(client, owner["project_id"], definition)
    assert (
        client.post(
            "/api/objects",
            json={"project_id": owner["project_id"], "type_key": "Project", "properties": {"name": "A"}},
        ).status_code
        == 201
    )
    change = client.post(
        "/api/changesets",
        json={
            "project_id": owner["project_id"],
            "title": "Next",
            "definition": definition,
            "expected_baseline_release_id": release["release_id"],
        },
    ).json()
    if background:
        monkeypatch.setattr("ontology.modeling.validation.SYNC_LIMIT", 0)
        monkeypatch.setattr("ontology.modeling.validation.BATCH", 1)
    change = client.post(
        f"/api/changesets/{change['id']}/validate", json={"expected_revision": change["revision"]}
    ).json()
    if background:
        assert process_one(app.state.db, app.state.settings)
    generation = change["validation"]["generation"]
    with app.state.db.transaction(owner["tenant_id"]) as session:
        assert session.scalar(select(EntityKey.id).where(EntityKey.definition_id == generation))
    response = client.patch(
        f"/api/changesets/{change['id']}", json={"expected_revision": change["revision"], "title": "Edited"}
    )
    assert response.status_code == 200, response.text
    with app.state.db.transaction(owner["tenant_id"]) as session:
        assert not session.scalar(select(EntityKey.id).where(EntityKey.definition_id == generation))
        assert session.scalar(select(EntityKey.id).where(EntityKey.definition_id == release["release_id"]))
        if background:
            assert session.get(Job, change["validation"]["job_id"]).status == "cancelled"
    # The published key remains enforced after disposing of draft-only data.
    assert (
        client.post(
            "/api/objects",
            json={"project_id": owner["project_id"], "type_key": "Project", "properties": {"name": "A"}},
        ).status_code
        == 409
    )
