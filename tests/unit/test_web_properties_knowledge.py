"""Only the new property and non-scientific boundaries, without network/ORCA."""

import json
from types import SimpleNamespace

import pytest
from test_agent import ScriptedTransport
from test_orca import FIXTURES, prepare_case, snapshot, synthetic_output

from orca_agent.config import Config
from orca_agent.context import _result
from orca_agent.entrypoints import create_question
from orca_agent.models import BudgetLimits, Goal, PermissionSnapshot, Request, Result
from orca_agent.natural import initialize_agent
from orca_agent.orca.adapter import read_outputs
from orca_agent.runner import execute
from orca_agent.store import Store
from orca_agent.tools.knowledge import DOCUMENTS, answer


def test_opt_failure_never_publishes_dipole(tmp_path):
    params, tool = prepare_case(tmp_path, "water_opt")
    (tmp_path / "stdout.out").write_text(synthetic_output(tmp_path / "geometry.xyz"))
    result = read_outputs(tmp_path, params, tool)
    assert "energy" in result["qualified_outputs"]
    assert "dipole_moment" not in result["qualified_outputs"]


def test_existing_real_file_read_does_not_create_or_modify_evidence():
    directory = FIXTURES / "real_water_sp"
    before = snapshot(directory)
    manifest = json.loads((directory / "input-manifest.json").read_text())
    read_outputs(directory, manifest["parameters"], manifest["tool"])
    assert snapshot(directory) == before


def test_retrieved_excerpt_is_visible_in_feedback():
    document = {"status": "found", "excerpt": "bounded scientific text " * 100,
                "url": DOCUMENTS["orca_electric"][1], "scientific_qualification": False}
    result = Result(run_id="run", operation_status="completed",
                    observations={"document_excerpt": document})
    assert _result(result, 1024)["unqualified_observations"]["document_excerpt"] == document


def test_knowledge_intent_uses_same_loop_and_cannot_claim_science(tmp_path):
    store = Store(tmp_path, environment_root=tmp_path / "environment")
    request = Request(original_text="Explain a dipole.",
        goals=[Goal(id="question", port="knowledge_answer", minimum_check_version="knowledge-answer-1")],
        conditions={"explain_results": True})
    permission = PermissionSnapshot(model_execution=True, allowed_tools=["knowledge.answer"])
    budget = BudgetLimits(orca_starts=0, extra_orca_starts=0, model_calls=3, model_tokens=48000,
        input_tokens=24000, output_tokens=2000, decision_rounds=4, evidence_reads=2)
    run = initialize_agent(store, Config(), request, permission, budget, defer_environment=True)
    transport = ScriptedTransport({"action": "knowledge.answer", "parameters": {
        "goal_id": "question", "answer": "A dipole describes separated charge."}}, {"action": "stop"})
    execute(store, Config(), run.id, transport=transport)
    run = store.load_run(run.id)
    assert run.state == "completed" and run.goal_status["question"] == "satisfied"
    result = store.load_result(run.id, run.result_ids[0])
    assert not result.qualified_outputs and run.usage.orca_starts_actual == 0
    assert result.observations["knowledge_answer"]["scientific_qualification"] is False
    config = Config(text={"enabled": True, "permission": permission, "budget": budget})
    followup, _ = create_question(store, config, run.id, "Explain again", submission_id="question_2")
    assert followup.budget.model_calls == 3 and followup.budget.orca_starts == 0
    assert not followup.permission.scientific_execution


def test_missing_requested_sources_rejected(tmp_path):
    store = Store(tmp_path, environment_root=tmp_path / "environment")
    request = Request(original_text="Explain with sources", goals=[Goal(id="q", port="knowledge_answer",
        minimum_check_version="knowledge-answer-1", conditions={"requires_sources": True})])
    run = store.create_run(request, None, PermissionSnapshot(), BudgetLimits())
    with pytest.raises(ValueError, match="requires actual retrieved sources"):
        answer(store, run, SimpleNamespace(parameters={"goal_id": "q", "answer": "unsupported",
                                                      "source_result_ids": [], "limitations": []}))


def test_optimized_property_report_names_final_geometry_not_job_input():
    from orca_agent.delivery import goal_fact_rows
    from orca_agent.models import Check, QualifiedOutput
    request = Request(goals=[Goal(id="dipole", port="dipole_moment",
                                 conditions={"geometry_relation": "optimized"})])
    checks = [Check(name="bound", status="passed")]
    result = Result(run_id="r", operation_status="completed", source={"geometry_artifact_id": "initial"},
        qualified_outputs={"dipole_moment": QualifiedOutput(value=1.7, unit="Debye", checks=checks),
                           "optimized_geometry": QualifiedOutput(artifact_id="final", checks=checks)})
    row = goal_fact_rows(request, SimpleNamespace(goal_status={"dipole": "satisfied"}),
        {"dipole": {"result": result, "assessment": {"status": "passed"}}})[0]
    assert row["source_geometry_artifact_id"] == "final"
    assert result.source["geometry_artifact_id"] == "initial"
