import copy
from types import SimpleNamespace

import pytest
from ontology.core.errors import DomainError
from ontology.documents.storage_models import Document
from ontology.identity.models import Membership
from ontology.intelligence.pipeline import normalize_facts
from ontology.intelligence.proposal_models import AIPage
from ontology.jobs.models import Job
from ontology.objects.models import Entity, Relation
from sqlalchemy import func, select
from tests.support.intelligence import (
    SyntheticModel,
    create_case,
    create_proposal,
    product_definition,
    run_worker,
)
from tests.support.postgres import pg_databases as pg_databases  # noqa: PLC0414
from tests.support.postgres import pg_platform as pg_platform  # noqa: PLC0414


@pytest.fixture(params=["platform", "pg_platform"])
def audit_platform(request):
    return request.getfixturevalue(request.param)


def test_worker_covers_all_pages_reuses_recognition_for_new_goal_and_imports(platform, monkeypatch):
    model = SyntheticModel(monkeypatch)
    case = create_case(platform)
    first = run_worker(case, create_proposal(case))
    assert first["status"] == "ready", first["job"]
    assert first["job"]["checkpoint"]["pages_completed"] == 3
    assert len(first["candidates"]) == 3 and len(first["relations"]) == 1
    assert {ref["page"] for c in first["candidates"] for ref in c["evidence"]} == {1, 2, 3}
    second = run_worker(case, create_proposal(case, instructions="只提取型号"))
    assert second["status"] == "ready", second["job"]
    assert len([call for call in model.calls if call[0] == "page"]) == 3
    assert not any("rating" in candidate["properties"] for candidate in second["candidates"])
    assert len([call for call in model.calls if call[0] == "facts"]) == 6
    client = case["client"]
    result = client.get(f"/api/ai/proposals/{first['id']}/pages", params={"limit": 1, "offset": 1}).json()
    assert result["total"] == 3 and len(result["items"]) == 1 and result["items"][0]["page_number"] == 2
    response = client.post(
        f"/api/ai/proposals/{first['id']}/adopt", json={"expected_revision": first["revision"]}
    )
    assert response.status_code == 200, response.text
    adopted, change = response.json()["proposal"], response.json()["changeset"]
    for operation in ("validate", "approve", "publish"):
        response = client.post(
            f"/api/changesets/{change['id']}/{operation}", json={"expected_revision": change["revision"]}
        )
        assert response.status_code == 200, response.text
        change = response.json()
    response = client.post(
        f"/api/ai/proposals/{first['id']}/import", json={"expected_revision": adopted["revision"]}
    )
    assert response.status_code == 200, response.text
    assert response.json()["imported"] == 3 and response.json()["relations"] == 1
    again = client.post(
        f"/api/ai/proposals/{first['id']}/import", json={"expected_revision": adopted["revision"]}
    )
    assert again.status_code == 200 and again.json()["imported"] == 3
    with case["app"].state.db.transaction(case["ids"]["tenant_id"]) as session:
        assert session.scalar(select(func.count()).select_from(Entity)) == 3
        assert session.scalar(select(func.count()).select_from(Relation)) == 1
        assert session.scalar(select(func.count()).select_from(AIPage)) == 3


def test_page_iteration_does_not_rehash_entire_document_for_each_page(platform, monkeypatch):
    from ontology.documents import pages
    from ontology.intelligence import pipeline

    SyntheticModel(monkeypatch)
    case = create_case(platform)
    original, hashes = pages.file_digest, []

    def digest(path):
        hashes.append(path)
        return original(path)

    monkeypatch.setattr(pages, "file_digest", digest)
    monkeypatch.setattr(pipeline, "file_digest", digest)
    result = run_worker(case, create_proposal(case))
    assert result["status"] == "ready"
    assert result["job"]["checkpoint"]["pages_completed"] == 3
    assert len(hashes) == 1


def test_logical_page_cache_tracks_fixed_parse_snapshot_and_new_goal_can_use_new_parse(audit_platform, monkeypatch):
    from ontology import worker
    from ontology.documents.history import capture_current_parse
    from tests.support.content import upload

    model = SyntheticModel(monkeypatch)
    case = create_case(audit_platform, pages=1)
    case["document"] = upload(case["client"], case["ids"]["project_id"], b"First interpretation", "evidence.txt")["document"]
    assert worker.process_one(case["app"].state.db, case["app"].state.settings)
    old = create_proposal(case)
    with case["app"].state.db.transaction(case["ids"]["tenant_id"], write=True) as session:
        doc = session.get(Document, case["document"]["id"])
        changed = copy.deepcopy(doc.ir)
        changed["elements"][0]["text"] = "Second interpretation"
        doc.ir, doc.ir_fingerprint = changed, None
        doc.parse_revision += 1
        capture_current_parse(session, doc)
    old = run_worker(case, old)
    assert old["status"] == "ready"
    locator = old["inputs"]["documents"][0]["native_parse_id"]
    assert old["candidates"][0]["evidence"][0]["native_parse_id"] == locator
    page_url = f"/api/documents/{case['document']['id']}/pages"
    assert case["client"].get(page_url, params={"native_parse_id": locator}).json()["items"][0]["text"] == "First interpretation"
    assert case["client"].get(page_url).json()["items"][0]["text"] == "Second interpretation"
    new = run_worker(case, create_proposal(case, instructions="只提取型号"))
    assert new["status"] == "ready"
    recognized = [call[2]["native_text"] for call in model.calls if call[0] == "page"]
    assert recognized == ["First interpretation", "Second interpretation"]
    again = run_worker(case, create_proposal(case))
    assert again["status"] == "ready"
    assert len([call for call in model.calls if call[0] == "page"]) == 2
    forged = copy.deepcopy(old["candidates"])
    forged[0]["evidence"][0]["native_parse_id"] = "native"
    response = case["client"].patch(f"/api/ai/proposals/{old['id']}", json={
        "expected_revision": old["revision"], "candidates": forged,
    })
    assert response.status_code == 422
    response = case["client"].post(f"/api/ai/proposals/{old['id']}/adopt", json={"expected_revision": old["revision"]})
    assert response.status_code == 200, response.text
    adopted, change = response.json()["proposal"], response.json()["changeset"]
    for operation in ("validate", "approve", "publish"):
        response = case["client"].post(f"/api/changesets/{change['id']}/{operation}", json={"expected_revision": change["revision"]})
        assert response.status_code == 200, response.text
        change = response.json()
    response = case["client"].post(f"/api/ai/proposals/{old['id']}/import", json={"expected_revision": adopted["revision"]})
    assert response.status_code == 200, response.text
    from ontology.sources.models import SourceRecordRevision
    with case["app"].state.db.transaction(case["ids"]["tenant_id"]) as session:
        record = session.scalar(select(SourceRecordRevision))
        assert record.values["__ontweft_field_evidence"]["code"][0]["native_parse_id"] == locator


def test_pending_native_parse_cannot_change_an_already_selected_logical_input(audit_platform, monkeypatch):
    from ontology import worker
    from tests.support.content import upload

    model = SyntheticModel(monkeypatch)
    case = create_case(audit_platform, pages=1)
    case["document"] = upload(case["client"], case["ids"]["project_id"], b"Native original", "evidence.txt")["document"]
    proposal = create_proposal(case)
    assert proposal["inputs"]["documents"][0]["native_parse_id"] is None
    assert worker.process_one(case["app"].state.db, case["app"].state.settings)
    with case["app"].state.db.transaction(case["ids"]["tenant_id"], write=True) as session:
        doc = session.get(Document, case["document"]["id"])
        changed = copy.deepcopy(doc.ir)
        changed["elements"][0]["text"] = "Different later interpretation"
        doc.ir = changed
    ready = run_worker(case, proposal)
    assert ready["status"] == "ready"
    assert [call[2]["native_text"] for call in model.calls if call[0] == "page"] == ["Native original"]
    assert ready["candidates"][0]["evidence"][0]["native_parse_id"] == "native"
    result = case["client"].get(f"/api/documents/{case['document']['id']}/pages", params={"native_parse_id": "native"})
    assert result.json()["items"][0]["text"] == "Native original"


def test_json_candidate_cannot_hide_nonfinite_value_inside_value_json():
    definition = product_definition()
    definition["types"][0]["properties"].append({"key": "details", "data_type": "json"})
    page = SimpleNamespace(id="parsed", document_id="doc", page_number=1, result={"text": "Synthetic A"})
    facts = {
        "candidates": [{"id": "a", "type_key": "Product", "business_key_fields": ["code"],
                        "values": [{"key": "code", "value_json": '"A"'},
                                   {"key": "details", "value_json": '{"value":NaN}'}],
                        "quotes": ["Synthetic A"]}],
        "relations": [], "gaps": [],
    }
    candidates, relations, gaps = normalize_facts(facts, page, definition)
    assert not candidates and not relations and gaps


def test_budget_stops_before_dispatch_and_retry_preserves_completed_pages(platform, monkeypatch):
    model = SyntheticModel(monkeypatch)
    case = create_case(platform)
    proposal = create_proposal(case, budget_cny=0.025)
    partial = run_worker(case, proposal)
    assert partial["status"] == "partial"
    assert partial["job"]["checkpoint"]["pages_completed"] == 2
    assert len(model.calls) == 2
    assert partial["job"]["checkpoint"]["reserved_cny"] == 0
    # A transient provider failure is retried through the existing job, keeping page checkpoints.
    model.cost = 0.0001
    assert case["client"].post(f"/api/jobs/{proposal['job_id']}/retry").status_code == 200
    ready = run_worker(case, proposal)
    assert ready["status"] == "ready", ready["job"]
    assert [call[2]["page"] for call in model.calls if call[0] == "page"] == [1, 2, 3]


def test_crash_after_page_checkpoints_resumes_without_rerecognition(platform, monkeypatch):
    model = SyntheticModel(monkeypatch)
    case = create_case(platform)

    def fail_model(kind, _payload):
        if kind == "model":
            raise DomainError(429, "model_rate_limited", "Synthetic rate limit")

    model.before_reserve = fail_model
    proposal = create_proposal(case)
    partial = run_worker(case, proposal)
    assert partial["status"] == "partial" and partial["job"]["checkpoint"]["pages_completed"] == 3
    model.before_reserve = None
    assert case["client"].post(f"/api/jobs/{proposal['job_id']}/retry").status_code == 200
    ready = run_worker(case, proposal)
    assert ready["status"] == "ready"
    assert len([call for call in model.calls if call[0] == "page"]) == 3


def test_revoked_permission_stops_next_request_and_prevents_results_access(platform, monkeypatch):
    model = SyntheticModel(monkeypatch)
    case = create_case(platform)
    proposal = create_proposal(case)

    def revoke(_kind, _payload):
        with case["app"].state.db.transaction(case["ids"]["tenant_id"], write=True) as session:
            member = session.scalar(select(Membership).where(Membership.user_id == case["ids"]["user_id"]))
            member.role = "viewer"

    model.after_reserve = revoke
    from ontology.worker import process_one

    assert process_one(case["app"].state.db, case["app"].state.settings)
    assert len(model.calls) == 1
    assert case["client"].get(f"/api/ai/proposals/{proposal['id']}").status_code == 403
    with case["app"].state.db.transaction(case["ids"]["tenant_id"]) as session:
        assert session.get(Job, proposal["job_id"]).status == "failed"
        assert session.scalar(select(func.count()).select_from(AIPage)) == 0


def test_old_results_remain_readable_but_cannot_be_adopted_after_source_change(platform, monkeypatch):
    SyntheticModel(monkeypatch)
    case = create_case(platform, pages=1)
    proposal = run_worker(case, create_proposal(case))
    with case["app"].state.db.transaction(case["ids"]["tenant_id"], write=True) as session:
        session.get(Document, case["document"]["id"]).revision += 1
    client = case["client"]
    assert client.get(f"/api/ai/proposals/{proposal['id']}").status_code == 200
    assert client.get(f"/api/ai/proposals/{proposal['id']}/pages").json()["total"] == 1
    assert (
        client.post(
            f"/api/ai/proposals/{proposal['id']}/adopt", json={"expected_revision": proposal["revision"]}
        ).status_code
        == 409
    )


@pytest.mark.parametrize(
    "mutation", ["invented_quote", "wrong_page", "business_key", "duplicate_id", "relation_type"]
)
def test_review_rejects_unbound_evidence_identity_and_relation_edits(platform, monkeypatch, mutation):
    SyntheticModel(monkeypatch)
    case = create_case(platform)
    proposal = run_worker(case, create_proposal(case))
    candidates, relations = copy.deepcopy(proposal["candidates"]), copy.deepcopy(proposal["relations"])
    if mutation == "invented_quote":
        candidates[0]["evidence"][0]["quote"] = "This does not occur in the source."
    elif mutation == "wrong_page":
        candidates[0]["evidence"][0]["page"] = 3
    elif mutation == "business_key":
        candidates[0]["business_key"] = {"code": "invented"}
    elif mutation == "duplicate_id":
        candidates.append(copy.deepcopy(candidates[0]))
    else:
        relations[0]["key"] = "invented"
    response = case["client"].patch(
        f"/api/ai/proposals/{proposal['id']}",
        json={"expected_revision": proposal["revision"], "candidates": candidates, "relations": relations},
    )
    assert response.status_code == 422, response.text


def test_missing_composite_identity_is_not_silently_reduced_and_decimal_is_exact():
    page = SimpleNamespace(
        id="parsed", document_id="document", page_number=1, result={"text": "A rating 0.1234567890123456789"}
    )
    data = {
        "candidates": [
            {
                "id": "a",
                "type_key": "Product",
                "values": [
                    {"key": "code", "value_json": '"A"'},
                    {"key": "rating", "value_json": "0.1234567890123456789"},
                ],
                "business_key_fields": ["code", "missing_part"],
                "quotes": [page.result["text"]],
            }
        ],
        "relations": [],
        "gaps": [],
    }
    items, _, gaps = normalize_facts(data, page, product_definition())
    assert items[0]["business_key"] == {} and not items[0]["selected"] and gaps
    assert items[0]["properties"]["rating"] == "0.1234567890123456789"


def test_page_selection_rejects_empty_and_foreign_document_and_deduplicates(platform):
    case = create_case(platform)
    base = {
        "project_id": case["ids"]["project_id"],
        "connection_id": case["connection"]["id"],
        "document_ids": [case["document"]["id"]],
    }
    for selection in ({case["document"]["id"]: []}, {"unselected-document": [1]}):
        assert case["client"].post("/api/ai/proposals", json={**base, "pages": selection}).status_code == 422
    proposal = create_proposal(case, pages={case["document"]["id"]: [2, 1, 2]})
    assert proposal["inputs"]["pages"][case["document"]["id"]] == [1, 2]


def test_proposal_response_exposes_progress_not_internal_samples(platform):
    case = create_case(platform)
    proposal = create_proposal(case)
    with case["app"].state.db.transaction(case["ids"]["tenant_id"], write=True) as session:
        session.get(Job, proposal["job_id"]).checkpoint = {
            "stage": "modeling",
            "pages_completed": 2,
            "source_profiles": [{"samples": [{"confidential": "synthetic sample"}]}],
            "definition": {"types": []},
        }
    detail = case["client"].get(f"/api/ai/proposals/{proposal['id']}").json()
    assert detail["job"]["checkpoint"] == {"stage": "modeling", "pages_completed": 2}
    listing = (
        case["client"]
        .get("/api/ai/proposals", params={"project_id": case["ids"]["project_id"]})
        .json()["items"]
    )
    assert "definition" not in listing[0]


def test_cancel_discards_inflight_result_and_retry_keeps_recognized_pages(platform, monkeypatch):
    model = SyntheticModel(monkeypatch)
    case = create_case(platform, pages=1)
    proposal = create_proposal(case)

    def cancel_inflight(kind, _payload):
        if kind == "model":
            response = case["client"].post(f"/api/jobs/{proposal['job_id']}/cancel")
            assert response.status_code == 200

    model.after_reserve = cancel_inflight
    cancelled = run_worker(case, proposal)
    assert cancelled["status"] == "cancelled"
    assert cancelled["candidates"] == []
    assert cancelled["job"]["checkpoint"]["pages_completed"] == 1
    model.after_reserve = None
    assert case["client"].post(f"/api/jobs/{proposal['job_id']}/retry").status_code == 200
    ready = run_worker(case, proposal)
    assert ready["status"] == "ready"
    assert len([call for call in model.calls if call[0] == "page"]) == 1


def test_stale_review_revision_cannot_overwrite_newer_changes(platform, monkeypatch):
    SyntheticModel(monkeypatch)
    case = create_case(platform, pages=1)
    proposal = run_worker(case, create_proposal(case))
    values = copy.deepcopy(proposal["candidates"])
    values[0]["selected"] = False
    url = f"/api/ai/proposals/{proposal['id']}"
    first = case["client"].patch(url, json={"expected_revision": proposal["revision"], "candidates": values})
    assert first.status_code == 200, first.text
    stale = case["client"].patch(
        url, json={"expected_revision": proposal["revision"], "candidates": proposal["candidates"]}
    )
    assert stale.status_code == 409
    assert not case["client"].get(url).json()["candidates"][0]["selected"]
