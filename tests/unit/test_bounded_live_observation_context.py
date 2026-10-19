"""Portable projections of real live006 feedback; no execution or transport."""

import copy
import json
from pathlib import Path

from orca_agent import context
from orca_agent.models import (
    BudgetLimits,
    Goal,
    OutputBinding,
    PermissionSnapshot,
    Plan,
    Request,
    Result,
    Run,
    Step,
)
from tests.unit.test_decision_purpose_capacity import decoded


def feedback():
    path = Path(__file__).parents[1] / "fixtures/code_contracts/live006-bounded-observation-feedback.json"
    return Result.model_validate(json.loads(path.read_bytes())["result"])


def test_actual_multirecord_preview_keeps_all_sources_and_explicit_manifest_lookup():
    result = feedback()
    before = result.model_copy(deep=True)
    projected = context._result(result, 256)
    rows = projected["unqualified_observation_records"]
    assert [row["name"] for row in rows] == [row.name for row in result.observation_records]
    for row, original in zip(rows, result.observation_records, strict=True):
        assert row["validation_status"] == original.validation_status == "unverified"
        assert row["limitations"] == original.limitations
        sources = copy.deepcopy(row["sources"])
        for source in sources:
            conditions = source.get("actual_conditions", {})
            if conditions.get("observation_actual_conditions_ref") == ".":
                source["actual_conditions"] = {**row["actual_conditions"], **conditions["additional_fields"]}
        assert sources == original.model_dump(mode="json")["sources"]
        if "value_projection" in row:
            assert row["value_projection"]["complete"] is False
            assert row["value_projection"]["source_ref"] == "observation_record_source"
    candidate = next(row for row in rows if row["name"] == "candidate_structures")
    assert candidate["value"] == {"count": 12}
    source = projected["observation_record_source"]
    assert source["artifact_id"] == "artifact_f6c203dfa1b7453f9aa389414eb65f5d"
    assert source["sha256"] == "cba072a67e62ef91da63c1d0432d35bcf6b43c29da9a29069b10d59743ab761b"
    assert source["path"] == [{"kind": "key", "key": "observations"}]
    assert "qualified_outputs" not in projected and result == before


def test_real_multirecord_values_fit_complete_request_and_keep_small_numeric_tables():
    result = feedback()
    goals = [Goal(id=f"goal_{i}", port=row.name,
        minimum_check_version="generic-observation-1", evidence_contract_version="generic-observation-1",
        verification="reported_with_limitations") for i, row in enumerate(result.observation_records)]
    request = Request(goals=goals, conditions={"explain_results": True})
    step = Step(id=result.step_id, logical_id="analysis", tool="python.execute", parameters={
        "code": {"artifact_id": "script_artifact", "sha256": result.observation_records[0].code_sha256},
        "observation_names": [row.name for row in result.observation_records]})
    plan = Plan(request_id=request.id, steps=[step], goal_map={goal.id:
        OutputBinding(step_id=step.id, port=goal.port) for goal in goals})
    run = Run(id=result.run_id, request_id=request.id, request_version=request.version,
        plan_id=plan.id, plan_version=plan.version,
        result_ids=[result.id], permission=PermissionSnapshot(model_execution=True,
            code_execution=True, artifact_writes=True, allowed_tools=["python.execute", "workspace.write"]),
        budget=BudgetLimits(model_calls=4, model_tokens=200000, input_tokens=32000,
            output_tokens=2000, plan_revisions=2))
    run.goal_status = dict.fromkeys((goal.id for goal in goals), "insufficient_evidence")
    snapshot = {"version": "test-bounded-feedback", "fingerprint": "a" * 64,
        "basis": {"request_id": request.id, "request_version": request.version, "plan_id": plan.id,
            "plan_version": plan.version, "permission_version": run.permission.version, "control_generation": 0},
        "request_text": request.original_text, "communication": {},
        "resources": {"permission": {}, "limits": {}, "remaining": {}, "usage": {}},
        "references": {"s0": {"run_id": run.id, "result_id": result.id}},
        "goals": [{"goal_id": goal.id, "ref": f"g{i}", "port": goal.port,
            "required_fact_refs": [f"f{i}"], "explanation_refs": [f"e{i}"],
            "required_blocker_refs": [], "next_action_refs": [f"n{i}"]} for i, goal in enumerate(goals)],
        "facts": [{"ref": f"f{i}", "goal_ref": f"g{i}", "source_ref": "s0", "kind": "answer",
            "value": {"kind": "evidence_observation", "observation": row.model_dump(mode="json")}}
            for i, row in enumerate(result.observation_records)],
        "explanations": [], "blockers": [], "next_actions": []}
    before = copy.deepcopy((request, run, plan, result, snapshot))
    prepared = context.build_context(request, run, plan, results=[result], delivery_snapshot=snapshot,
        feedback={"new_result_ids": [result.id], "pending_step_ids": []})
    wire = decoded(prepared)
    assert prepared.input_token_bound <= 32000
    observation = next(fact["value"]["observation"] for fact in wire["DATA"]["delivery"]["facts"]
        if fact["value"]["observation"]["name"] == "lowest_candidate_energy_ranking")
    original = next(row for row in result.observation_records if row.name == observation["name"])
    assert observation["value"] == original.value
    assert len(observation["value"]["ranking"]) == 4
    assert (request, run, plan, result, snapshot) == before
    assert not run.model_records and run.usage.model_calls == 0
