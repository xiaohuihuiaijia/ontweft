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
