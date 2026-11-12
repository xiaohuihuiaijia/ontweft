import time

import httpx
import pytest
from ontology.connectors.policy import NetworkPolicy
from ontology.core.errors import DomainError
from ontology.intelligence.client import ModelCallError
from ontology.intelligence.models import ModelConnection
from ontology.intelligence.pipeline import Run
from ontology.jobs.models import Job
from ontology.jobs.service import claim_for_tenant
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


@pytest.mark.parametrize("scenario", ["budget", "429", "timeout", "success", "known_unknown", "unknown_known"])
def test_schema_repair_settles_each_response_without_releasing_unknown_requests(platform, monkeypatch, scenario):
    case = create_case(platform, pages=1)
    db, tenant = case["app"].state.db, case["ids"]["tenant_id"]
    with db.transaction(tenant) as session:
        connection = session.get(ModelConnection, case["connection"]["id"])
        connection.models = [{"id": "synthetic", "max_output_tokens": 1024, "context_window": 32768,
                              "input_price_per_million": 1, "output_price_per_million": 1}]
        connection.roles = {"extraction": "synthetic", "synthesis": "synthetic"}
    proposal = create_proposal(case, budget_cny=0.002 if scenario == "budget" else 1)
    claim = claim_for_tenant(db, tenant, "budget-test", 300)
    run = Run(db, case["app"].state.settings, claim)
    monkeypatch.setattr(NetworkPolicy, "check_url", lambda *args: None)
    reservations, calls = [], []
    reserve = run.reserve

    def record_reserve(charge):
        reserve(charge)
        reservations.append(charge["estimated_cost"])

    run.reserve = record_reserve

    def response(request):
        calls.append(request)
        if len(calls) == 2 and scenario == "429":
            return httpx.Response(429)
        if len(calls) == 2 and scenario == "timeout":
            raise httpx.ReadTimeout("Synthetic timeout", request=request)
        body = {"model": "synthetic", "choices": [{"message": {"content": "not json" if len(calls) == 1 else '{"ok":true}'}, "finish_reason": "stop"}]}
        if not ((scenario == "known_unknown" and len(calls) == 2) or (scenario == "unknown_known" and len(calls) == 1)):
            body["usage"] = {"prompt_tokens": 100, "completion_tokens": 20}
        return httpx.Response(200, json=body)

    run.client.transport = httpx.MockTransport(response)
    schema = {"type": "object", "properties": {"ok": {"type": "boolean"}}, "required": ["ok"], "additionalProperties": False}
    if scenario in {"budget", "429", "timeout"}:
        with pytest.raises(DomainError) as caught:
            run.generate("extraction", "synthetic", "synthetic", schema)
        assert caught.value.code == {"budget": "ai_budget_exhausted", "429": "model_rate_limited", "timeout": "model_timeout"}[scenario]
    else:
        assert run.generate("extraction", "synthetic", "synthetic", schema).data == {"ok": True}
    with db.transaction(tenant) as session:
        checkpoint = session.get(Job, proposal["job_id"]).checkpoint
    assert len(calls) == (1 if scenario == "budget" else 2)
    assert checkpoint["cost_cny"] == pytest.approx(0.00024 if scenario == "success" else 0.00012)
    expected_reserved = (reservations[-1] if scenario in {"429", "timeout", "known_unknown"}
                         else reservations[0] if scenario == "unknown_known" else 0)
    assert checkpoint["reserved_cny"] == pytest.approx(expected_reserved)
    assert checkpoint["usage"]["total_tokens"] == (240 if scenario == "success" else 120)


@pytest.mark.parametrize("interruption", ["cancel", "expired_lease"])
def test_received_charge_does_not_bypass_cancel_or_expired_lease(platform, monkeypatch, interruption):
    case = create_case(platform, pages=1)
    proposal = create_proposal(case)
    db, tenant = case["app"].state.db, case["ids"]["tenant_id"]
    run = Run(db, case["app"].state.settings, claim_for_tenant(db, tenant, "fenced-test", 300))
    monkeypatch.setattr(NetworkPolicy, "check_url", lambda *args: None)
    calls = []

    def response(request):
        calls.append(request)
        if interruption == "cancel":
            assert case["client"].post(f"/api/jobs/{proposal['job_id']}/cancel").status_code == 200
        else:
            with db.transaction(tenant, write=True) as session:
                session.get(Job, proposal["job_id"]).lease_until = time.time() - 1
        return httpx.Response(200, json={"model": "synthetic", "choices": [
            {"message": {"content": '{"ok":true}'}, "finish_reason": "stop"}
        ], "usage": {"prompt_tokens": 100, "completion_tokens": 20}})

    run.client.transport = httpx.MockTransport(response)
    with pytest.raises(DomainError) as caught:
        run.generate("extraction", "synthetic", "synthetic", {"type": "object"})
    assert caught.value.status == 409 and len(calls) == 1
    with db.transaction(tenant) as session:
        job = session.get(Job, proposal["job_id"])
        assert job.checkpoint["reserved_cny"] > 0
        assert job.checkpoint.get("cost_cny", 0) == 0
        assert job.status == ("cancelled" if interruption == "cancel" else "running")
