import json

import pytest
from ontology.intelligence.client import GenerationResult, ModelCallError
from ontology.intelligence.prompts import FACT_SCHEMA
from ontology.jobs.models import Job
from tests.support.intelligence import SyntheticModel, create_case, create_proposal, run_worker


class DenseModel(SyntheticModel):
    def __init__(self, monkeypatch, *, lines=18):
        super().__init__(monkeypatch)
        self.texts = {
            **self.texts,
            2: "".join(f"P{number:03d} is a synthetic product.\n" for number in range(lines)),
        }
        self.behavior = "right_truncated"

    def generate(self, role, **kwargs):
        result = super().generate(role, **kwargs)
        payload = json.loads(kwargs["text"])
        if kwargs["schema"] != FACT_SCHEMA or payload["page"] != 2:
            return result
        start, end = payload["focus_start"], payload["focus_end"]
        full = payload["text"]
        if self.behavior == "legacy_failure":
            if start > 0:
                raise ModelCallError(429, "model_rate_limited", "Synthetic limit", cost=self.cost)
        elif self.behavior != "success" and (
            (start, end) == (0, len(full)) or self.behavior == "all_truncated" or start > 0
        ):
            raise ModelCallError(
                422, "model_output_truncated", "Synthetic truncation", usage=result.usage, cost=self.cost
            )
        candidates, offset = [], 0
        for line in full.splitlines(keepends=True):
            if start <= offset < end:
                code = line.split(" ", 1)[0]
                candidates.append(
                    {
                        "id": code,
                        "type_key": "Product",
                        "values": [{"key": "code", "value_json": json.dumps(code)}],
                        "business_key_fields": ["code"],
                        "quotes": [line.strip()],
                    }
                )
            offset += len(line)
        return GenerationResult(
            {"candidates": candidates, "relations": [], "gaps": []},
            result.model,
            result.usage,
            cost=result.cost,
        )


def stored_checkpoint(case, proposal):
    with case["app"].state.db.transaction(case["ids"]["tenant_id"]) as session:
        return session.get(Job, proposal["job_id"]).checkpoint


def test_truncated_leaf_stops_once_and_retry_keeps_completed_sibling_and_prior_page(platform, monkeypatch):
    model = DenseModel(monkeypatch)
    case = create_case(platform, pages=2)
    proposal = create_proposal(case)
    partial = run_worker(case, proposal)
    assert partial["status"] == "partial" and "8192" in partial["job"]["error"]
    checkpoint = stored_checkpoint(case, proposal)
    assert len(checkpoint["facts_completed"]) == 1
    with case["app"].state.db.transaction(case["ids"]["tenant_id"]) as session:
        assert session.get(Job, proposal["job_id"]).result["error_code"] == "model_output_truncated"
    blocked = checkpoint["facts_blocked_range"]
    ranges = checkpoint["facts_ranges"][blocked["parse_id"]]
    assert len(ranges) == 2 and all(segment["split"] for segment in ranges)
    assert ranges[0]["id"] in checkpoint["facts_segments_completed"]
    assert ranges[1]["id"] not in checkpoint["facts_segments_completed"]
    assert ranges[0]["start"] == 0 and ranges[0]["end"] == ranges[1]["start"]
    assert ranges[1]["end"] == len(model.texts[2])
    assert "synthetic product" not in json.dumps(checkpoint["facts_ranges"])
    assert len(partial["candidates"]) > 1
    calls = [call[2] for call in model.calls if call[0] == "facts" and call[2]["page"] == 2]
    assert len(calls) == 3  # root + left success + right failure; no recursive repair loop
    assert all(call["text"] == model.texts[2] for call in calls)
    assert all(call["previous_pages"][0]["text"] == model.texts[1] for call in calls)
    assert calls[1]["focus_text"] + calls[2]["focus_text"] == model.texts[2]
    model.behavior = "success"
    assert case["client"].post(f"/api/jobs/{proposal['job_id']}/retry").status_code == 200
    ready = run_worker(case, proposal)
    assert ready["status"] == "ready", ready["job"]
    assert len(ready["candidates"]) == 19
    assert len({candidate["id"] for candidate in ready["candidates"]}) == 19
    all_facts = [call[2] for call in model.calls if call[0] == "facts"]
    assert len([call for call in all_facts if call["page"] == 1]) == 1
    assert len([call for call in all_facts if call["page"] == 2 and call["focus_start"] == 0]) == 2
    assert all_facts[-1]["focus_start"] == blocked["start"]
    checkpoint = stored_checkpoint(case, proposal)
    assert len(checkpoint["facts_completed"]) == 2 and "facts_blocked_range" not in checkpoint
    jobs = case["client"].get("/api/jobs", params={"project_id": case["ids"]["project_id"]}).json()["items"]
    job = next(row for row in jobs if row["id"] == proposal["job_id"])
    assert "facts_ranges" not in job["checkpoint"] and "facts_blocked_range" not in job["checkpoint"]


def test_repeated_truncation_never_splits_a_child_again(platform, monkeypatch):
    model = DenseModel(monkeypatch)
    model.behavior = "all_truncated"
    case = create_case(platform, pages=2)
    proposal = create_proposal(case)
    partial = run_worker(case, proposal)
    assert partial["status"] == "partial"
    checkpoint = stored_checkpoint(case, proposal)
    ranges = checkpoint["facts_ranges"][checkpoint["facts_blocked_range"]["parse_id"]]
    assert len(ranges) == 2
    assert len([call for call in model.calls if call[0] == "facts" and call[2]["page"] == 2]) == 2
    assert case["client"].post(f"/api/jobs/{proposal['job_id']}/retry").status_code == 200
    run_worker(case, proposal)
    assert len([call for call in model.calls if call[0] == "facts" and call[2]["page"] == 2]) == 3
    assert stored_checkpoint(case, proposal)["facts_ranges"] == checkpoint["facts_ranges"]


def test_split_children_still_reserve_budget_before_dispatch(platform, monkeypatch):
    model = DenseModel(monkeypatch)
    case = create_case(platform, pages=2)
    proposal = create_proposal(case, budget_cny=0.055)
    partial = run_worker(case, proposal)
    assert partial["status"] == "partial" and "预算不足" in partial["job"]["error"]
    checkpoint = stored_checkpoint(case, proposal)
    assert checkpoint["cost_cny"] == pytest.approx(0.05)
    assert checkpoint["reserved_cny"] == 0
    page = next(key for key in checkpoint["facts_ranges"] if key not in checkpoint["facts_completed"])
    assert len(checkpoint["facts_ranges"][page]) == 2
    assert len([call for call in model.calls if call[0] == "facts" and call[2]["page"] == 2]) == 1
    model.cost, model.behavior = 0.001, "success"
    assert case["client"].post(f"/api/jobs/{proposal['job_id']}/retry").status_code == 200
    ready = run_worker(case, proposal)
    assert ready["status"] == "ready" and len(ready["candidates"]) == 19
    assert stored_checkpoint(case, proposal)["cost_cny"] == pytest.approx(0.052)


def test_legacy_completed_segment_ids_resume_without_reextracting_first_segment(platform, monkeypatch):
    model = DenseModel(monkeypatch, lines=95)
    model.behavior = "legacy_failure"
    case = create_case(platform, pages=2)
    proposal = create_proposal(case)
    partial = run_worker(case, proposal)
    assert partial["status"] == "partial"
    with case["app"].state.db.transaction(case["ids"]["tenant_id"], write=True) as session:
        job = session.get(Job, proposal["job_id"])
        # Reproduce the pre-adaptive checkpoint shape, leaving its already stored candidates intact.
        job.checkpoint = {key: value for key, value in job.checkpoint.items() if key != "facts_ranges"}
    model.behavior = "success"
    assert case["client"].post(f"/api/jobs/{proposal['job_id']}/retry").status_code == 200
    ready = run_worker(case, proposal)
    assert ready["status"] == "ready" and len(ready["candidates"]) == 96
    first_segments = [
        call
        for call in model.calls
        if call[0] == "facts" and call[2]["page"] == 2 and call[2]["focus_start"] == 0
    ]
    assert len(first_segments) == 1
