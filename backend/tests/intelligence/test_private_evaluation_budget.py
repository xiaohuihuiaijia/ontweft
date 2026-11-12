import importlib.util

import pytest
from tests.support.paths import REPO_ROOT

spec = importlib.util.spec_from_file_location(
    "ontweft_private_evaluation", REPO_ROOT / "scripts/evaluate_private_document.py"
)
evaluation = importlib.util.module_from_spec(spec)
spec.loader.exec_module(evaluation)


@pytest.mark.parametrize(
    ("previous_budget", "own", "others", "expected"),
    [
        (20, {"cost_cny": 2}, [{"cost_cny": 3}, {"cost_cny": 4}], 13),
        (20, {"cost_cny": 2, "reserved_cny": 5}, [{"cost_cny": 3}], 17),
        (20, {"cost_cny": 2}, [{"cost_cny": 3, "reserved_cny": 5}], 12),
        (6, {"cost_cny": 2}, [{"cost_cny": 3}], 6),
        (20, {"cost_cny": 2, "reserved_cny": 5}, [{"cost_cny": 13}], 7),
    ],
)
def test_retry_shares_total_budget_and_preserves_unknown_cost_reservations(
    previous_budget, own, others, expected
):
    cap = evaluation.retry_budget_cny(previous_budget, 20, [own, *others], own)
    assert cap == expected
    assert cap <= previous_budget
    assert cap + sum(cp.get("cost_cny", 0) + cp.get("reserved_cny", 0) for cp in others) <= 20


def test_resume_requires_the_same_document_and_page_scope():
    inputs = {
        "documents": [{"id": "synthetic-manual"}],
        "pages": {"synthetic-manual": [2, 4]},
        "quality": "balanced",
        "instructions": "Synthetic asset extraction",
        "sources": [],
    }
    matches = lambda value, doc="synthetic-manual", pages=(2, 4): evaluation.matches_evaluation(
        value, doc, pages, "balanced", "Synthetic asset extraction"
    )
    assert matches(inputs, pages=(4, 2, 2))
    assert not matches(inputs, pages=(3, 6))
    assert not matches(inputs, pages=(2,))
    assert not matches(inputs, doc="other-manual")
    assert not matches({**inputs, "pages": {}})
    assert not matches({**inputs, "sources": [{"id": "synthetic-database"}]})
    assert not matches({**inputs, "quality": "high"})
    assert not matches({**inputs, "instructions": "Another goal"})
