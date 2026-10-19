"""Input checks must not become a scientific qualification in the final facts."""

import pytest
from test_general_input_goals_live004 import completed_input_chain

from orca_agent.delivery import goal_fact_rows
from orca_agent.goals import goal_evidence_assessment
from orca_agent.report import build_report, render_report


@pytest.mark.parametrize("port,rule", [("prepared_geometry", "structure-prepare-2"),
                                      ("resolved_identity", "structure-identity-2")])
def test_actual_input_chain_is_delivered_as_checked_input_only(tmp_path, monkeypatch, port, rule):
    store, run, request, goal, prepared, identity = completed_input_chain(tmp_path, monkeypatch)
    result = prepared if port == "prepared_geometry" else identity
    goal = goal.model_copy(update={"port": port, "minimum_check_version": rule})
    request = request.model_copy(update={"goals": [goal]})
    assessment = goal_evidence_assessment(store, run, request, goal, result)
    assert assessment["status"] == "passed"
    run.goal_status[goal.id] = "satisfied"
    row, = goal_fact_rows(request, run, {goal.id: {"result": result, "assessment": assessment}})
    assert row["goal_complete"] is True
    assert row["answer"]["kind"] == "qualified_input_artifact"
    assert row["answer"]["scientific_qualification"] is False
    assert row["answer"]["check_versions"] == [rule]
    assert row["answer"]["artifact_id"] == result.qualified_outputs[port].artifact_id
    assert not run.attempts and run.usage.orca_starts_actual == 0
    report = build_report(store, run)
    report["goal_facts"] = [row]
    rendered = render_report(report)
    assert "本目标已检查输入（不宣称科学资格）" in rendered
    assert row["answer"]["artifact_id"] in rendered and rule in rendered
    assert "本目标合格输出" not in rendered
