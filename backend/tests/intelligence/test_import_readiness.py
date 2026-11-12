"""Import hints track the same adoption/release gate as the actual operation."""

import copy

import pytest
from ontology.documents.storage_models import Document
from tests.support.intelligence import SyntheticModel, create_case, create_proposal, run_worker
from tests.support.platform import publish
from tests.support.postgres import pg_databases as pg_databases  # noqa: PLC0414
from tests.support.postgres import pg_platform as pg_platform  # noqa: PLC0414


@pytest.fixture(params=["platform", "pg_platform"])
def readiness_platform(request):
    return request.getfixturevalue(request.param)


def adopt(case, proposal):
    response = case["client"].post(
        f"/api/ai/proposals/{proposal['id']}/adopt", json={"expected_revision": proposal["revision"]}
    )
    assert response.status_code == 200, response.text
    return response.json()["proposal"], response.json()["changeset"]


def finish_publish(client, change):
    for operation in ("validate", "approve", "publish"):
        response = client.post(
            f"/api/changesets/{change['id']}/{operation}", json={"expected_revision": change["revision"]}
        )
        assert response.status_code == 200, response.text
        change = response.json()
    return change


def detail(case, proposal):
    response = case["client"].get(f"/api/ai/proposals/{proposal['id']}")
    assert response.status_code == 200, response.text
    return response.json()


def assert_blocked(case, proposal, reason):
    row = detail(case, proposal)
    assert row["import_ready"] is False
    assert reason in row["import_block_reason"]
    response = case["client"].post(
        f"/api/ai/proposals/{row['id']}/import", json={"expected_revision": row["revision"]}
    )
    assert response.status_code == 409, response.text
    assert response.json()["error"]["message"] == row["import_block_reason"]


def test_import_hint_changes_after_adoption_publication_and_idempotent_import(readiness_platform, monkeypatch):
    SyntheticModel(monkeypatch)
    case = create_case(readiness_platform, pages=1)
    queued = create_proposal(case)
    assert queued["import_ready"] is False and "采纳" in queued["import_block_reason"]
    proposal = run_worker(case, queued)
    assert_blocked(case, proposal, "采纳")
    proposal, change = adopt(case, proposal)
    assert proposal["import_ready"] is False and "发布" in proposal["import_block_reason"]
    assert_blocked(case, proposal, "发布")
    finish_publish(case["client"], change)
    ready = detail(case, proposal)
    assert ready["import_ready"] is True and ready["import_block_reason"] is None
    response = case["client"].post(
        f"/api/ai/proposals/{proposal['id']}/import", json={"expected_revision": proposal["revision"]}
    )
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["imported"] == 1
    assert result["proposal"]["import_ready"] is False
    assert "已导入" in result["proposal"]["import_block_reason"]
    again = case["client"].post(
        f"/api/ai/proposals/{proposal['id']}/import", json={"expected_revision": proposal["revision"]}
    )
    assert again.status_code == 200 and again.json()["snapshots"] == result["snapshots"]


def test_no_new_changeset_can_import_but_later_release_blocks_both_paths(readiness_platform, monkeypatch):
    SyntheticModel(monkeypatch)
    case = create_case(readiness_platform, pages=1)
    original, change = adopt(case, run_worker(case, create_proposal(case)))
    finish_publish(case["client"], change)
    reused, no_change = adopt(case, run_worker(case, create_proposal(case)))
    assert no_change is None and reused["changeset_id"] is None
    assert reused["import_ready"] is True and reused["import_block_reason"] is None
    response = case["client"].post(
        f"/api/ai/proposals/{reused['id']}/import", json={"expected_revision": reused["revision"]}
    )
    assert response.status_code == 200, response.text
    pending, no_change = adopt(case, run_worker(case, create_proposal(case)))
    assert no_change is None and pending["import_ready"] is True
    newer = copy.deepcopy(pending["review_definition"])
    newer["types"][0]["label"] = "已修订的产品"
    publish(case["client"], case["ids"]["project_id"], newer)
    assert_blocked(case, original, "发布")
    assert_blocked(case, pending, "发布")


def test_model_only_proposal_does_not_offer_document_import(readiness_platform, monkeypatch):
    SyntheticModel(monkeypatch)
    case = create_case(readiness_platform, pages=1)
    proposal = run_worker(case, create_proposal(case))
    response = case["client"].patch(f"/api/ai/proposals/{proposal['id']}", json={
        "expected_revision": proposal["revision"], "candidates": [], "relations": [],
    })
    assert response.status_code == 200, response.text
    proposal, change = adopt(case, response.json())
    finish_publish(case["client"], change)
    assert_blocked(case, proposal, "文档实例")


def test_ready_hint_does_not_bypass_execution_time_source_version_check(readiness_platform, monkeypatch):
    SyntheticModel(monkeypatch)
    case = create_case(readiness_platform, pages=1)
    proposal, change = adopt(case, run_worker(case, create_proposal(case)))
    finish_publish(case["client"], change)
    with case["app"].state.db.transaction(case["ids"]["tenant_id"], write=True) as session:
        session.get(Document, case["document"]["id"]).revision += 1
    ready = detail(case, proposal)
    assert ready["import_ready"] is True and ready["import_block_reason"] is None
    response = case["client"].post(
        f"/api/ai/proposals/{proposal['id']}/import", json={"expected_revision": proposal["revision"]}
    )
    assert response.status_code == 409
    assert "来源文档版本已改变" in response.json()["error"]["message"]
