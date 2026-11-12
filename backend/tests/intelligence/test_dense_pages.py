from ontology.core.errors import DomainError
from ontology.intelligence.pipeline import fact_segments
from tests.support.intelligence import SyntheticModel, create_case, create_proposal, run_worker


def test_dense_page_segmentation_preserves_every_character_and_table_row():
    text = "| Code | Rating | Unit |\n" + "| SYN-001 | 24 | V |\n" * 220
    segments = fact_segments(text)
    assert len(segments) > 1 and "".join(segments) == text
    assert all(segment.endswith("\n") for segment in segments)
    assert "".join(fact_segments("x" * 7000)) == "x" * 7000


def test_failed_dense_page_resumes_completed_segments_and_keeps_original_evidence(platform, monkeypatch):
    model = SyntheticModel(monkeypatch)
    model.texts = {1: "Product A has rating 12.50.\n" * 150}
    case = create_case(platform, pages=1)
    proposal = create_proposal(case)

    def fail_second_part(kind, payload):
        if kind == "facts" and payload["part"] == 2:
            raise DomainError(503, "synthetic_failure", "Synthetic temporary failure")

    model.before_reserve = fail_second_part
    first = run_worker(case, proposal)
    assert first["status"] == "partial"
    assert first["job"]["checkpoint"]["pages_completed"] == 1
    assert first["job"]["checkpoint"]["facts_pages_completed"] == 0
    model.before_reserve = None
    assert case["client"].post(f"/api/jobs/{proposal['job_id']}/retry").status_code == 200
    completed = run_worker(case, proposal)
    assert completed["status"] == "ready"
    assert completed["job"]["checkpoint"]["facts_pages_completed"] == 1
    assert len([call for call in model.calls if call[0] == "page"]) == 1
    calls = [call[2] for call in model.calls if call[0] == "facts"]
    assert [call["part"] for call in calls] == list(range(1, len(fact_segments(model.texts[1])) + 1))
    assert all(call["text"] == model.texts[1] for call in calls)
    assert len(completed["candidates"]) == 1
    assert completed["candidates"][0]["evidence"][0]["page"] == 1
