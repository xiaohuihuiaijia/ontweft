"""Draft document mappings must not mutate the active source catalog."""

import copy

import pytest
from ontology.intelligence.proposal_models import AIProposal
from ontology.objects.contributions.models import ContributionRevision, SourceContribution
from ontology.objects.models import Entity
from ontology.sources.models import Source, SourceIdentity, SourceRecordRevision
from sqlalchemy import select
from tests.support.intelligence import SyntheticModel, create_case, create_proposal, run_worker
from tests.support.platform import publish
from tests.support.postgres import pg_databases as pg_databases  # noqa: PLC0414
from tests.support.postgres import pg_platform as pg_platform  # noqa: PLC0414


@pytest.fixture(params=["platform", "pg_platform"])
def lifecycle_platform(request):
    return request.getfixturevalue(request.param)


def adopt(case, proposal):
    response = case["client"].post(
        f"/api/ai/proposals/{proposal['id']}/adopt", json={"expected_revision": proposal["revision"]}
    )
    assert response.status_code == 200, response.text
    return response.json()["proposal"], response.json()["changeset"]


def transition(case, change, operations=("validate", "approve", "publish")):
    for operation in operations:
        response = case["client"].post(
            f"/api/changesets/{change['id']}/{operation}", json={"expected_revision": change["revision"]}
        )
        assert response.status_code == 200, response.text
        change = response.json()
        assert change["status"] != "draft", change
    return change


def imported(case, proposal):
    response = case["client"].post(
        f"/api/ai/proposals/{proposal['id']}/import", json={"expected_revision": proposal["revision"]}
    )
    assert response.status_code == 200, response.text
    return response.json()


def extra_field():
    return {"key": "rating_copy", "label": "Printed rating", "data_type": "number"}


def snapshot(case, source_id):
    with case["app"].state.db.transaction(case["ids"]["tenant_id"]) as session:
        source = session.get(Source, source_id)
        return copy.deepcopy(source.resources), source.revision


@pytest.mark.parametrize("asynchronous", [False, True])
def test_other_draft_leaves_published_source_usable_until_publish(lifecycle_platform, monkeypatch, asynchronous):
    from ontology import worker
    from ontology.modeling import validation

    SyntheticModel(monkeypatch)
    case = create_case(lifecycle_platform, pages=1)
    first, change = adopt(case, run_worker(case, create_proposal(case)))
    assert snapshot(case, first["source_id"])[0] == []
    transition(case, change)
    original_catalog = snapshot(case, first["source_id"])
    second = run_worker(case, create_proposal(case))
    definition, candidates = copy.deepcopy(second["definition"]), copy.deepcopy(second["candidates"])
    definition["types"][0]["properties"].append(extra_field())
    candidates[0]["properties"]["rating_copy"] = candidates[0]["properties"]["rating"]
    response = case["client"].patch(f"/api/ai/proposals/{second['id']}", json={
        "expected_revision": second["revision"], "definition": definition, "candidates": candidates,
    })
    assert response.status_code == 200, response.text
    second, pending = adopt(case, response.json())
    assert second["source_id"] == first["source_id"]
    assert snapshot(case, first["source_id"]) == original_catalog
    assert imported(case, first)["imported"] == 1
    with case["app"].state.db.transaction(case["ids"]["tenant_id"]) as session:
        identity_id = session.scalar(select(SourceIdentity.id))
        entity_id = session.scalar(select(Entity.id))
        record_id = session.scalar(select(SourceRecordRevision.id))
        contribution_id = session.scalar(select(SourceContribution.id))

    # Another reviewed subset may still use the old active release while B is only a draft.
    waiting, no_change = adopt(case, run_worker(case, create_proposal(case)))
    assert no_change is None
    if asynchronous:
        with monkeypatch.context() as limited:
            limited.setattr(validation, "SYNC_LIMIT", 0)
            pending = transition(case, pending, ("validate",))
            assert pending["status"] == "validating"
            for _ in range(3):
                assert worker.process_one(case["app"].state.db, case["app"].state.settings)
                pending = case["client"].get(f"/api/changesets/{pending['id']}").json()
                if pending["status"] == "validated":
                    break
            assert pending["status"] == "validated"
    else:
        pending = transition(case, pending, ("validate",))
    assert snapshot(case, first["source_id"]) == original_catalog
    transition(case, pending, ("approve", "publish"))
    resources, revision = snapshot(case, first["source_id"])
    assert revision == original_catalog[1] + 1
    assert "rating_copy" in {field["name"] for field in resources[0]["fields"]}
    stale = case["client"].post(f"/api/ai/proposals/{waiting['id']}/import", json={"expected_revision": waiting["revision"]})
    assert stale.status_code == 409
    result = imported(case, second)
    assert result["imported"] == 1 and result["counts"]["updated"] == 1
    with case["app"].state.db.transaction(case["ids"]["tenant_id"]) as session:
        assert session.scalar(select(SourceIdentity.id)) == identity_id
        assert session.scalar(select(Entity.id)) == entity_id
        assert session.get(SourceRecordRevision, record_id) is not None
        assert session.get(Entity, entity_id).properties["rating_copy"] == "12.50"
        assert session.scalar(select(SourceContribution.id)) == contribution_id
        assert session.scalar(select(ContributionRevision.id).where(
            ContributionRevision.contribution_id == contribution_id,
            ContributionRevision.record_id == record_id,
        )) is not None


@pytest.mark.parametrize("new_mapping", [False, True])
def test_model_only_adoption_can_reselect_instances(lifecycle_platform, monkeypatch, new_mapping):
    SyntheticModel(monkeypatch)
    case = create_case(lifecycle_platform, pages=1)
    initial = run_worker(case, create_proposal(case))
    definition = copy.deepcopy(initial["definition"])
    definition["types"][0]["properties"].append(extra_field())
    response = case["client"].patch(f"/api/ai/proposals/{initial['id']}", json={
        "expected_revision": initial["revision"], "definition": definition,
    })
    assert response.status_code == 200, response.text
    _, change = adopt(case, response.json())
    transition(case, change)
    proposal = run_worker(case, create_proposal(case))
    path = f"/api/ai/proposals/{proposal['id']}"
    candidates = copy.deepcopy(proposal["candidates"])
    candidates[0]["selected"] = False
    response = case["client"].patch(path, json={"expected_revision": proposal["revision"], "candidates": candidates})
    assert response.status_code == 200, response.text
    proposal, no_change = adopt(case, response.json())
    assert no_change is None and proposal["source_id"] is None
    candidates[0]["selected"] = True
    if new_mapping:
        candidates[0]["properties"]["rating_copy"] = candidates[0]["properties"]["rating"]
    response = case["client"].patch(path, json={"expected_revision": proposal["revision"], "candidates": candidates})
    assert response.status_code == 200, response.text
    proposal = response.json()
    assert proposal["source_id"] is not None
    if new_mapping:
        assert proposal["changeset_id"] and not proposal["import_ready"]
        change = case["client"].get(f"/api/changesets/{proposal['changeset_id']}").json()
        transition(case, change)
    else:
        assert proposal["changeset_id"] is None and proposal["import_ready"]
    assert imported(case, proposal)["imported"] == 1


def test_reselect_does_not_rebase_an_adopted_proposal_silently(lifecycle_platform, monkeypatch):
    SyntheticModel(monkeypatch)
    case = create_case(lifecycle_platform, pages=1)
    first, change = adopt(case, run_worker(case, create_proposal(case)))
    transition(case, change)
    proposal, no_change = adopt(case, run_worker(case, create_proposal(case)))
    assert no_change is None
    definition = copy.deepcopy(proposal["definition"])
    definition["types"][0]["label"] = "Updated label"
    publish(case["client"], case["ids"]["project_id"], definition)
    before = snapshot(case, first["source_id"])
    response = case["client"].patch(f"/api/ai/proposals/{proposal['id']}", json={
        "expected_revision": proposal["revision"], "candidates": proposal["candidates"],
    })
    assert response.status_code == 409
    assert snapshot(case, first["source_id"]) == before


def test_publication_rejects_resources_not_bound_to_validation(lifecycle_platform, monkeypatch):
    SyntheticModel(monkeypatch)
    case = create_case(lifecycle_platform, pages=1)
    proposal, change = adopt(case, run_worker(case, create_proposal(case)))
    change = transition(case, change, ("validate", "approve"))
    with case["app"].state.db.transaction(case["ids"]["tenant_id"], write=True) as session:
        row = session.get(AIProposal, proposal["id"])
        inputs = copy.deepcopy(row.inputs)
        inputs["document_resources"][0]["fields"][1]["metadata"]["label"] = "Unreviewed catalog edit"
        row.inputs = inputs
    response = case["client"].post(f"/api/changesets/{change['id']}/publish", json={"expected_revision": change["revision"]})
    assert response.status_code == 409
    assert snapshot(case, proposal["source_id"])[0] == []


def test_legacy_prepared_source_without_draft_resource_metadata_still_publishes(lifecycle_platform, monkeypatch):
    SyntheticModel(monkeypatch)
    case = create_case(lifecycle_platform, pages=1)
    proposal, change = adopt(case, run_worker(case, create_proposal(case)))
    # Model the previous release's storage contract, not a migration of existing data.
    with case["app"].state.db.transaction(case["ids"]["tenant_id"], write=True) as session:
        row = session.get(AIProposal, proposal["id"])
        inputs = copy.deepcopy(row.inputs)
        source = session.get(Source, proposal["source_id"])
        source.resources = inputs.pop("document_resources")
        row.inputs = inputs
    original_catalog = snapshot(case, proposal["source_id"])
    transition(case, change)
    assert snapshot(case, proposal["source_id"]) == original_catalog
    assert imported(case, proposal)["imported"] == 1
