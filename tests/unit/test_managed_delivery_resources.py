"""Managed execution costs remain distinct from historical terminal facts."""

from orca_agent.delivery import _science_resources
from orca_agent.models import (
    Attempt,
    BudgetLimits,
    BudgetUsage,
    InternalCall,
    PermissionSnapshot,
    Run,
)


def test_managed_terminal_keeps_exhaustion_unknown_occupancy_and_incomplete_cost():
    run = Run(request_id="request", request_version=1, permission=PermissionSnapshot(code_execution=True),
        budget=BudgetLimits(internal_work_units=9, code_retries=1),
        usage=BudgetUsage(internal_work_units=9, code_retries=1, cpu_seconds=2.5,
                          resource_usage_complete=False),
        internal_calls=[InternalCall(request_key="pending", input_fingerprint="a" * 64,
                                     state="unknown")])
    facts = _science_resources(run)
    assert facts["remaining"]["internal_work_units"] == 0
    assert facts["remaining"]["code_retries"] == 0
    assert facts["usage"]["internal_work_units"] == 9
    assert facts["unsettled_internal_request_keys"] == ["pending"]
    assert facts["execution_cost"]["cpu_seconds"] == 2.5
    assert facts["execution_cost"]["complete"] is False
    assert facts["artifact_usage"] == {"status": "not_in_snapshot", "disk_hard_quota": False}


def test_legacy_terminal_does_not_acquire_new_execution_facts_or_permission():
    facts = _science_resources(Run(request_id="historical", request_version=1))
    assert "code_execution" not in facts["permission"]
    assert "internal_work_units" not in facts["limits"]
    assert "execution_cost" not in facts
    assert "artifact_usage" not in facts


def test_general_observation_does_not_inherit_legacy_checker_restrictions():
    from orca_agent.models import Goal, Request
    from orca_agent.natural import _missing_information

    request = Request(method="PBE", basis="def2-SVP", conditions={"environment": "water_solvent"},
        goals=[Goal(id="spectrum", port="vibrational_frequencies", quantity="vibrational frequencies",
                    minimum_check_version="generic-observation-1",
                    evidence_contract_version="generic-observation-1", verification="reported_with_limitations")])
    normalized = _missing_information(request)
    assert normalized.method == "PBE" and normalized.conditions == request.conditions
    assert normalized.goals == request.goals
    assert normalized.normalization_status == "normalized"


def test_historical_extra_code_attempts_remain_occupied_without_rewriting_counter():
    run = Run(request_id="request", request_version=1, budget=BudgetLimits(code_retries=2),
        attempts=[Attempt(step_id=f"s{i}", logical_id=f"l{i}", number=1, tool="python.execute",
                          execution_kind="python", state="failed", input_fingerprint=str(i),
                          directory=f"attempt-{i}") for i in range(3)])
    before = run.model_dump_json()
    facts = _science_resources(run)
    assert facts["usage"]["code_retries"] == 0
    assert facts["code_attempt_allowance"]["effective_used"] == 2
    assert facts["remaining"]["code_retries"] == 0
    assert run.model_dump_json() == before
