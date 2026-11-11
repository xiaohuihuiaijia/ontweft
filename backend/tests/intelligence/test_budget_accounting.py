import pytest
from ontology.intelligence.client import ModelCallError
from tests.support.intelligence import SyntheticModel, create_case, create_proposal, run_worker


@pytest.mark.parametrize("reported", [True, False])
def test_unusable_answer_records_known_cost_or_retains_unknown_reservation(platform, monkeypatch, reported):
    model = SyntheticModel(monkeypatch)
    case = create_case(platform, pages=1)

    def fail_after_charge(_kind, _payload):
        raise ModelCallError(
            422,
            "model_output_truncated",
            "Synthetic incomplete output",
            usage={"input_tokens": 20, "output_tokens": 30} if reported else {},
            cost=0.004 if reported else None,
        )

    model.after_reserve = fail_after_charge
    result = run_worker(case, create_proposal(case))
    assert result["job"]["status"] == "failed"
    checkpoint = result["job"]["checkpoint"]
    assert checkpoint.get("cost_cny", 0) == (0.004 if reported else 0)
    assert checkpoint["reserved_cny"] == (0 if reported else model.cost)
    assert checkpoint.get("usage", {}).get("output_tokens", 0) == (30 if reported else 0)
    assert len(model.calls) == 1


def test_actual_model_and_duration_are_retained_without_boolean_token_counts(platform, monkeypatch):
    model = SyntheticModel(monkeypatch)
    original = model.generate

    def measured(*args, **kwargs):
        result = original(*args, **kwargs)
        result.elapsed_ms = 7
        result.usage["reported"] = True
        return result

    model.generate = measured
    case = create_case(platform, pages=1)
    result = run_worker(case, create_proposal(case))
    assert result["status"] == "ready"
    checkpoint = result["job"]["checkpoint"]
    assert checkpoint["models_used"] == {"synthetic-model": {"calls": 3, "elapsed_ms": 21}}
    assert "reported" not in checkpoint["usage"]
