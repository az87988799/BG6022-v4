"""Only an explicit new delivery policy may report unsupported obligations."""

from copy import deepcopy

import pytest

from orca_agent.minimum_evidence import assess_minimum_evidence
from orca_agent.models import Check, Goal, Result


def evidence(requirements, **goal_fields):
    goal = Goal(id="goal_energy", port="energy", minimum_check_version="generic-observation-1",
        evidence_contract_version="generic-observation-1", verification="reported_with_limitations",
        minimum_evidence=requirements,
        minimum_evidence_source=dict.fromkeys(requirements, "explicit"),
        text_evidence={"message_id": "message_user", "text_basis": "保存全部候选并报告能量排名",
                       "shared_constraints": [{"text_basis": "使用种子1701", "start": 30, "end": 39}]})
    for key, value in goal_fields.items():
        setattr(goal, key, value)
    return goal, Result(run_id="run_test", step_id="step_test", operation_status="completed")


def test_default_assessment_keeps_exact_historical_strict_shape():
    goal, result = evidence(["保存至少8份候选"])
    expected = [{"requested": "保存至少8份候选", "canonical": "保存至少8份候选",
        "mapping_version": "minimum-evidence-1", "status": "unresolved",
        "reason": "unsupported_minimum_evidence"}]
    assert assess_minimum_evidence(goal, result, purpose_passed=True) == expected
    assert assess_minimum_evidence(goal, result, purpose_passed=True, reported_delivery=False) == expected


def test_reported_unknowns_keep_original_obligations_and_sources_without_passing():
    texts = ["保存至少8份候选", "考虑碳链正反等价映射的RMSD不得小于0.15Å", "报告kJ/mol排名"]
    goal, result = evidence(texts)
    before = deepcopy(goal)
    rows = assess_minimum_evidence(goal, result, purpose_passed=True, reported_delivery=True)
    assert [row["requested"] for row in rows] == texts
    for row in rows:
        assert row["status"] == "unverified" and row["reason"] == "no_registered_evaluator"
        assert row["blocking"] is False
        assert row["source"] == "explicit"
        assert row["text_evidence_ref"] == {"goal_id": goal.id, "field": "text_evidence"}
        assert "text_evidence" not in row
    rows[0]["text_evidence_ref"]["goal_id"] = "changed_projection"
    assert goal == before and rows[1]["text_evidence_ref"]["goal_id"] == goal.id


@pytest.mark.parametrize("requirement", ["energy", "generic-observation-1", "converged_scf@1",
                                          "purpose_preserved@1"])
def test_registered_requirement_missing_or_failed_purpose_still_blocks(requirement):
    goal, result = evidence([requirement])
    row, = assess_minimum_evidence(goal, result, purpose_passed=False, reported_delivery=True)
    assert row["status"] == "unresolved" and row["blocking"] is True
    assert row["reason"] == "minimum_evidence_missing_failed_or_wrong_version"


@pytest.mark.parametrize("status,version", [("failed", "generic-observation-1"), ("passed", "wrong-version")])
def test_registered_failed_or_wrong_version_check_is_not_downgraded_to_a_report(status, version):
    goal, result = evidence(["converged SCF", "generic-observation-1"])
    result.checks["energy"] = [Check(name="scf_converged", status=status, rule_version=version)]
    rows = assess_minimum_evidence(goal, result, purpose_passed=True, reported_delivery=True)
    assert all(row["status"] == "unresolved" and row["blocking"] is True for row in rows)


@pytest.mark.parametrize("fields", [{"verification": "qualified"}, {"evidence_contract_version": "legacy-1"}])
def test_qualified_and_legacy_goals_cannot_use_reported_unknown_evidence(fields):
    goal, result = evidence(["独立验证所有构象"], **fields)
    row, = assess_minimum_evidence(goal, result, purpose_passed=True, reported_delivery=True)
    assert row["status"] == "unresolved" and row["blocking"] is True
    assert row["reason"] == "unsupported_minimum_evidence"


def test_successful_registered_requirement_retains_its_actual_pass():
    goal, result = evidence(["purpose_preserved@1"])
    row, = assess_minimum_evidence(goal, result, purpose_passed=True, reported_delivery=True)
    assert row["status"] == "passed" and row["reason"] is None and row["blocking"] is False
