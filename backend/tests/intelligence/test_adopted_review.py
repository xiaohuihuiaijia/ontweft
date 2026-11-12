"""Candidate edits remain bound to the actual reviewed document mapping."""

import copy

import pytest
from ontology.identity.models import User
from ontology.identity.security import access
from ontology.intelligence.projection import import_proposal
from ontology.intelligence.proposal_models import AIProposal
from ontology.modeling.models import Release
from ontology.objects.models import Entity
from ontology.sources.models import SourceRecordRevision
from sqlalchemy import select
from tests.support.intelligence import SyntheticModel, create_case, create_proposal, run_worker
from tests.support.postgres import pg_databases as pg_databases  # noqa: PLC0414
from tests.support.postgres import pg_platform as pg_platform  # noqa: PLC0414


@pytest.fixture(params=["platform", "pg_platform"])
def review_platform(request):
    return request.getfixturevalue(request.param)


def transition(client, change, operations):
    for operation in operations:
        response = client.post(
            f"/api/changesets/{change['id']}/{operation}", json={"expected_revision": change["revision"]}
        )
        assert response.status_code == 200, response.text
        change = response.json()
    return change


def test_adopted_new_property_updates_same_review_and_imports_after_reapproval(review_platform, monkeypatch):
    SyntheticModel(monkeypatch)
    case = create_case(review_platform, pages=1)
    client, app, ids = case["client"], case["app"], case["ids"]
    proposal = run_worker(case, create_proposal(case))
    response = client.post(
        f"/api/ai/proposals/{proposal['id']}/adopt", json={"expected_revision": proposal["revision"]}
    )
    assert response.status_code == 200, response.text
    proposal, change = response.json()["proposal"], response.json()["changeset"]
    original = copy.deepcopy(proposal["definition"])
    definition = copy.deepcopy(change["definition"])
    definition["types"][0]["properties"].append(
        {
            "key": "rating_label",
            "label": "Rating as printed",
            "data_type": "string",
            "required": False,
        }
    )
    response = client.patch(
        f"/api/changesets/{change['id']}",
        json={"expected_revision": change["revision"], "definition": definition},
    )
    assert response.status_code == 200, response.text
    change = transition(client, response.json(), ("validate", "approve"))
    proposal = client.get(f"/api/ai/proposals/{proposal['id']}").json()
    assert proposal["review_changeset_revision"] == change["revision"]
    candidates = copy.deepcopy(proposal["candidates"])
    candidates[0]["properties"]["rating_label"] = "12.50"
    patch = {
        "expected_revision": proposal["revision"],
        "expected_changeset_revision": change["revision"],
        "candidates": candidates,
    }
    response = client.patch(f"/api/ai/proposals/{proposal['id']}", json=patch)
    assert response.status_code == 200, response.text
    reviewed = response.json()
    assert reviewed["definition"] == original
    assert reviewed["review_changeset_revision"] == change["revision"] + 1
    assert "rating_label" in reviewed["review_definition"]["mappings"][0]["fields"]
    assert client.patch(f"/api/ai/proposals/{proposal['id']}", json=patch).status_code == 409
    change = client.get(f"/api/changesets/{change['id']}").json()
    assert change["status"] == "draft" and change["approved_by"] is None and change["validation"] == {}
    assert (
        client.post(
            f"/api/ai/proposals/{proposal['id']}/import", json={"expected_revision": reviewed["revision"]}
        ).status_code
        == 409
    )
    change = transition(client, change, ("validate", "approve", "publish"))
    response = client.post(
        f"/api/ai/proposals/{proposal['id']}/import", json={"expected_revision": reviewed["revision"]}
    )
    assert response.status_code == 200, response.text
    assert response.json()["imported"] == 1
    with app.state.db.transaction(ids["tenant_id"]) as session:
        assert session.scalar(select(Entity)).properties["rating_label"] == "12.50"
        assert session.scalar(select(SourceRecordRevision)).values["rating_label"] == "12.50"
        assert session.get(AIProposal, proposal["id"]).definition == original
        assert (
            "rating_label" in session.get(Release, change["release_id"]).definition["mappings"][0]["fields"]
        )


def test_published_unmapped_property_is_rejected_without_rewriting_release(review_platform, monkeypatch):
    SyntheticModel(monkeypatch)
    case = create_case(review_platform, pages=1)
    client, app, ids = case["client"], case["app"], case["ids"]
    proposal = run_worker(case, create_proposal(case))
    response = client.post(
        f"/api/ai/proposals/{proposal['id']}/adopt", json={"expected_revision": proposal["revision"]}
    )
    assert response.status_code == 200, response.text
    change = response.json()["changeset"]
    # The field exists in the release but has no document mapping: no silent drop is allowed.
    definition = copy.deepcopy(change["definition"])
    definition["types"][0]["properties"].append(
        {"key": "rating_label", "label": "Label", "data_type": "string"}
    )
    response = client.patch(
        f"/api/changesets/{change['id']}",
        json={"expected_revision": change["revision"], "definition": definition},
    )
    assert response.status_code == 200, response.text
    change = transition(client, response.json(), ("validate", "approve", "publish"))
    proposal = client.get(f"/api/ai/proposals/{proposal['id']}").json()
    candidates = copy.deepcopy(proposal["candidates"])
    candidates[0]["properties"]["rating_label"] = "12.50"
    response = client.patch(
        f"/api/ai/proposals/{proposal['id']}",
        json={
            "expected_revision": proposal["revision"],
            "expected_changeset_revision": change["revision"],
            "candidates": candidates,
        },
    )
    assert response.status_code == 409, response.text
    assert response.json()["error"]["code"] == "document_candidate_unmapped"
    with app.state.db.transaction(ids["tenant_id"]) as session:
        stored = session.get(AIProposal, proposal["id"])
        assert "rating_label" not in stored.candidates[0]["properties"]
        release = session.get(Release, change["release_id"])
        assert "rating_label" not in release.definition["mappings"][0]["fields"]
        assert session.scalar(select(SourceRecordRevision.id)) is None
        # Unchanged candidates still use the actual reviewed release and import normally.
        user = session.get(User, ids["user_id"])
        ctx = access(session, user, ids["project_id"], "sources:write")
        assert import_proposal(session, stored, ctx)["imported"] == 1
