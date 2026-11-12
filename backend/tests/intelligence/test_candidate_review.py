import copy

import pytest
from ontology.core.errors import DomainError
from ontology.intelligence.proposal_api import check_candidates


def history():
    evidence = [{"document_id": "doc", "page": 1, "quote": "Synthetic A", "parse_id": "page"}]
    candidates = [{"id": "a", "type_key": "Removed", "properties": {"code": "A"},
                   "business_key": {"code": "A"}, "selected": False, "evidence": evidence}]
    relations = [{"id": "r", "source_candidate_id": "a", "target_candidate_id": "a",
                  "key": "removed_relation", "selected": False, "evidence": evidence}]
    return candidates, relations


def test_excluded_candidates_survive_removing_their_model_without_changing_evidence():
    candidates, relations = history()
    before = copy.deepcopy((candidates, relations))
    check_candidates({"types": [], "mappings": []}, candidates, relations)
    assert (candidates, relations) == before
    candidates[0]["selected"] = True
    with pytest.raises(DomainError) as error:
        check_candidates({"types": [], "mappings": []}, candidates, relations)
    assert error.value.code == "invalid_ai_candidate"


def test_reselecting_removed_relation_or_excluded_endpoint_is_rejected():
    candidates, relations = history()
    definition = {"types": [{"key": "Removed", "properties": [{"key": "code", "data_type": "string"}],
                              "relations": []}]}
    candidates[0]["selected"] = True
    relations[0]["selected"] = True
    with pytest.raises(DomainError) as error:
        check_candidates(definition, candidates, relations)
    assert error.value.code == "invalid_ai_relation"
    definition["types"][0]["relations"] = [{"key": "removed_relation", "target_type": "Removed"}]
    candidates[0]["selected"] = False
    with pytest.raises(DomainError) as error:
        check_candidates(definition, candidates, relations)
    assert error.value.code == "invalid_ai_relation"


@pytest.mark.parametrize("duplicate", ["candidate", "relation"])
def test_excluded_history_still_requires_unique_ids(duplicate):
    candidates, relations = history()
    (candidates if duplicate == "candidate" else relations).append(
        copy.deepcopy(candidates[0] if duplicate == "candidate" else relations[0])
    )
    with pytest.raises(DomainError):
        check_candidates({"types": []}, candidates, relations)


def test_api_preserves_excluded_evidence_when_the_suggested_type_is_removed(platform, monkeypatch):
    from tests.support.intelligence import SyntheticModel, create_case, create_proposal, run_worker

    SyntheticModel(monkeypatch)
    case = create_case(platform, pages=1)
    proposal = run_worker(case, create_proposal(case))
    candidates = [{**item, "selected": False} for item in proposal["candidates"]]
    relations = [{**item, "selected": False} for item in proposal["relations"]]
    path = f"/api/ai/proposals/{proposal['id']}"
    response = case["client"].patch(path, json={
        "expected_revision": proposal["revision"], "definition": {"types": [], "mappings": []},
        "candidates": candidates, "relations": relations,
    })
    assert response.status_code == 200, response.text
    revised = response.json()
    assert revised["candidates"] == candidates and revised["relations"] == relations
    candidates[0]["selected"] = True
    response = case["client"].patch(path, json={"expected_revision": revised["revision"], "candidates": candidates})
    assert response.status_code == 422, response.text
    unchanged = case["client"].get(path).json()
    assert unchanged["revision"] == revised["revision"]
    assert unchanged["candidates"][0]["selected"] is False


def test_adopted_review_uses_current_changeset_and_rejects_stale_review_revision(platform, monkeypatch):
    from tests.support.intelligence import SyntheticModel, create_case, create_proposal, run_worker

    SyntheticModel(monkeypatch)
    case = create_case(platform, pages=1)
    proposal = run_worker(case, create_proposal(case))
    client = case["client"]
    path = f"/api/ai/proposals/{proposal['id']}"
    response = client.post(path + "/adopt", json={"expected_revision": proposal["revision"]})
    assert response.status_code == 200, response.text
    proposal, change = response.json()["proposal"], response.json()["changeset"]
    changed_definition = copy.deepcopy(change["definition"])
    changed_definition["types"][0]["properties"].append(
        {"key": "review_note", "label": "审核备注", "data_type": "string", "required": True}
    )
    response = client.patch(f"/api/changesets/{change['id']}", json={
        "expected_revision": change["revision"], "definition": changed_definition,
    })
    assert response.status_code == 200, response.text
    detail = client.get(path).json()
    assert detail["review_changeset_revision"] == change["revision"] + 1
    assert "review_note" in {item["key"] for item in detail["review_definition"]["types"][0]["properties"]}
    assert "review_note" not in {item["key"] for item in detail["definition"]["types"][0]["properties"]}
    response = client.patch(path, json={
        "expected_revision": proposal["revision"], "expected_changeset_revision": change["revision"],
        "candidates": proposal["candidates"],
    })
    assert response.status_code == 409, response.text
    assert client.get(path).json()["revision"] == proposal["revision"]
