"""New paragraph retrieval and native terminal intent only; no live calls."""

import json

import pytest
from test_agent import ScriptedTransport

from orca_agent.config import Config
from orca_agent.context import build_context
from orca_agent.delivery import collect_delivery_snapshot
from orca_agent.models import BudgetLimits, Goal, PermissionSnapshot, Request, Step
from orca_agent.natural import initialize_agent
from orca_agent.planning import PlanningError, _step_permissions
from orca_agent.runner import execute
from orca_agent.store import Store, StoreError
from orca_agent.tools.knowledge import _Text, matched_excerpt


def test_paragraph_search_preserves_inline_text_and_prefers_exact_quantity():
    parser = _Text()
    parser.feed('<nav>dipole moment</nav><article><p>The <b>dipole</b> moment is a vector.</p>'
                '<p>Dipole-dipole polarizability is a tensor.</p><p>other topic</p></article>')
    text, indices = matched_excerpt(parser.parts, "dipole moment")
    assert parser.parts[0] == "The dipole moment is a vector."
    assert indices[0] == 0 and text.startswith(parser.parts[0])
    assert "other topic" not in text
    assert matched_excerpt(parser.parts, "unavailable_field") == ("", [])


def test_native_terminal_roundtrip_keeps_strict_contract_and_non_scientific_delivery(tmp_path):
    store = Store(tmp_path, environment_root=tmp_path / "environment")
    request = Request(original_text="Explain polarity", goals=[Goal(id="q", port="knowledge_answer",
        minimum_check_version="knowledge-answer-1")], conditions={"explain_results": True})
    run = initialize_agent(store, Config(), request,
        PermissionSnapshot(model_execution=True, allowed_tools=["knowledge.answer"]),
        BudgetLimits(orca_starts=0, extra_orca_starts=0, model_calls=3, model_tokens=48000,
            input_tokens=24000, output_tokens=2000, decision_rounds=4, evidence_reads=2), defer_environment=True)
    transport = ScriptedTransport({"action": "knowledge.answer", "parameters": {
        "goal_id": "q", "answer": "Charge separation produces a dipole."}}, {"action": "stop"})
    execute(store, Config(), run.id, transport=transport)
    run = store.load_run(run.id)
    assert run.state == "completed" and run.usage.orca_starts_actual == 0
    assert transport.sent[-1]["AUTHORITY"]["response_contract"] == "decision-intent-2"
    assert "SCHEMA_COLUMNS" not in transport.sent[-1]
    assert run.terminal_deliveries[-1].contract_status == "passed"
    assert not store.load_result(run.id, run.result_ids[0]).qualified_outputs
    answer_tool = next(t for t in transport.sent[0]["TOOLS"] if t["name"] == "knowledge.answer")
    assert "parameters" not in answer_tool
    assert answer_tool["parameters_ref"] == "ACTION_SCHEMAS.knowledge.answer"


def test_document_counter_cannot_decrease(tmp_path):
    store = Store(tmp_path, environment_root=tmp_path / "environment")
    run = store.create_run(Request(goals=[Goal(id="q", port="knowledge_answer",
        minimum_check_version="knowledge-answer-1")]), None, PermissionSnapshot(), BudgetLimits(knowledge_queries=2))
    run.usage.knowledge_queries = 1
    store.save_run(run)
    run.usage.knowledge_queries = 0
    with pytest.raises(StoreError, match="cumulative usage cannot decrease"):
        store.save_run(run)


def test_file_question_keeps_names_and_attempts_without_speculative_answer_plan(tmp_path):
    store = Store(tmp_path, environment_root=tmp_path / "environment")
    inventory = [{"artifact_id": "worker_stdout", "filename": "stdout.out", "attempt_id": None},
                 {"artifact_id": "orca_stdout", "filename": "stdout.out", "attempt_id": "attempt_science"},
                 {"artifact_id": "geometry", "filename": "job.xyz", "attempt_id": "attempt_science"}]
    request = Request(original_text="Read stdout.out first 10 lines", goals=[Goal(id="q",
        port="knowledge_answer", minimum_check_version="knowledge-answer-1")], conditions={
        "read_only_source_run": "source", "authorized_artifacts": inventory})
    run = initialize_agent(store, Config(), request, PermissionSnapshot(model_execution=True,
        allowed_tools=["knowledge.answer", "evidence.text"], artifact_ids=[r["artifact_id"] for r in inventory]),
        BudgetLimits(orca_starts=0, model_calls=4, model_tokens=48000, input_tokens=24000,
            output_tokens=2000, evidence_reads=4), defer_environment=True)
    wire = json.loads(build_context(request, run, relevant_tools=run.permission.allowed_tools,
        delivery_snapshot=collect_delivery_snapshot(store, run, request)).body()["messages"][1]["content"])
    assert wire["AUTHORITY"]["response_contract"] == "decision-intent-2"
    assert "initial_plan" not in wire["ACTION_SCHEMAS"]
    assert "evidence.text" in wire["ACTION_SCHEMAS"]
    visible = wire["AUTHORITY"]["request"]["conditions"]
    assert [r["basename"] for r in visible["authorized_artifacts"]] == ["stdout.out", "stdout.out"]
    assert visible["authorized_artifacts"][1]["attempt_id"] == "attempt_science"
    assert visible["artifact_index_coverage"]["total"] == 3
    assert wire["AUTHORITY"]["permission"]["artifact_ids"] == run.permission.artifact_ids
    assert request.conditions["authorized_artifacts"] == inventory
    step = Step(id="answer", logical_id="answer", tool="knowledge.answer",
                parameters={"goal_id": "q", "answer": "pending evidence read"})
    with pytest.raises(PlanningError, match="immediate decision"):
        _step_permissions(step, run)
