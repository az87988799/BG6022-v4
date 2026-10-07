"""Clarification serves current user scope; stopping never certifies science."""

import json
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from orca_agent import agent
from orca_agent.config import Config
from orca_agent.context import build_context
from orca_agent.models import BudgetLimits, Goal, PermissionSnapshot, Request, SystemInput
from orca_agent.report import build_report
from orca_agent.semantic import action_parameters
from orca_agent.store import Store, sha256_file
from tests.unit.test_agent import ScriptedTransport
from tests.unit.test_context import payload

_PROJECT = Path(__file__).resolve().parents[2]
_REGISTRATION_TEXT = "算一下这个水的单点电子能。采用气相 RHF/STO-3G，中性单重态。本轮只登记需求，不启动计算。"
_POLICY = ("Only clarify unknowns blocking current user scope", "Never reconfirm explicit choices",
           "registration/no execution")


def _run(tmp_path, scope="registration"):
    store = Store(tmp_path / "data", environment_root=tmp_path / "environment")
    artifact = store.import_artifact(_PROJECT / "tests/fixtures/phase_a/water_sp/geometry.xyz", "initial_geometry")
    texts = {
        "registration": _REGISTRATION_TEXT,
        "unknown_spin": "登记水的气相 RHF/STO-3G 单点电子能需求，电荷0；多重度还未确定，本轮不执行。",
        "permission_missing": "计算登记的水分子气相 RHF/STO-3G 中性单重态固定几何电子能。",
    }
    conditions = {"environment": "gas", "electronic_state": "RHF", "explain_results": True}
    sources = {key: "explicit" for key in ("geometry", "charge", "multiplicity", "method", "basis",
                                          "environment", "electronic_state")}
    if scope == "unknown_spin":
        sources["multiplicity"] = "unknown"
    request = Request(original_text=texts[scope], geometry_artifact_id=artifact.id,
        multiplicity=None if scope == "unknown_spin" else 1,
        conditions=conditions, conditions_source=sources,
        normalization_status="structured" if scope == "unknown_spin" else "normalized",
        unresolved=["field:multiplicity"] if scope == "unknown_spin" else [],
        condition_evidence={"request.charge": {"value": 0, "source": "explicit",
                                               "text_basis": "电荷0" if scope == "unknown_spin" else "中性"}},
        systems=[SystemInput(id="water", geometry_artifact_id=artifact.id)],
        goals=[Goal(id="water_energy", port="energy", minimum_check_version="orca-hf-2",
                    original_text="水的单点电子能", system_ids=["water"],
                    conditions={"geometry_relation": "fixed_initial"}, minimum_evidence=["converged_scf@1"])])
    permission = PermissionSnapshot(model_execution=True, scientific_execution=False, allowed_tools=[],
                                    artifact_ids=[artifact.id])
    budget = BudgetLimits(orca_starts=0, extra_orca_starts=0, model_calls=2, model_tokens=32000,
                          input_tokens=12000, output_tokens=2000, decision_rounds=2,
                          evidence_reads=0, analysis_executions=0)
    run = store.create_run(request, None, permission, budget)
    run.agent_enabled = True
    store.save_run(run)
    return store, run, request


@pytest.mark.parametrize("scope", ["registration", "unknown_spin", "permission_missing"])
def test_no_tool_context_keeps_stop_and_clarify_without_changing_user_scope(tmp_path, scope):
    _, run, request = _run(tmp_path, scope)
    before = request.model_dump_json(), run.model_dump_json()
    prepared = build_context(request, run)
    data = payload(prepared)
    prompt = prepared.body()["messages"][0]["content"]
    assert all(part in prompt for part in _POLICY)
    assert set(data["ACTION_PARAMETERS"]) == {"clarify", "stop"}
    assert data["TOOL_CATALOG"] == []
    assert data["AUTHORITY"]["user_originals"][0]["text"] == request.original_text
    assert data["AUTHORITY"]["request"]["goals"] == [goal.model_dump(
        mode="json", exclude={"unresolved", "identity", "text_evidence"})
        for goal in request.goals]
    assert data["AUTHORITY"]["request"]["conditions_source"] == request.conditions_source
    assert data["AUTHORITY"]["request"]["condition_evidence"] == request.condition_evidence
    assert data["AUTHORITY"]["permission"] == run.permission.model_dump(mode="json", exclude={
        "external_identity_queries", "geometry_preparation"})
    assert not run.permission.external_identity_queries and not run.permission.geometry_preparation
    if scope == "unknown_spin":
        assert data["AUTHORITY"]["request"]["multiplicity"] is None
        assert data["AUTHORITY"]["request"]["unresolved"] == ["field:multiplicity"]
    else:
        assert data["AUTHORITY"]["request"]["normalization_status"] == "normalized"
        assert data["AUTHORITY"]["request"]["unresolved"] == []
    assert (request.model_dump_json(), run.model_dump_json()) == before


@pytest.mark.parametrize("phase", ["semantic_intake", "final", "permitted_read"])
def test_no_tool_clarification_guidance_does_not_replace_other_phase_contracts(tmp_path, phase):
    _, run, request = _run(tmp_path)
    options = {}
    if phase == "semantic_intake":
        request.normalization_status = "pending"
        options["action_parameters"] = action_parameters(request=request)
    elif phase == "final":
        run.goal_status = {"water_energy": "satisfied"}
    else:
        run.permission.allowed_tools = ["evidence.value"]
        run.budget.evidence_reads = 1
        options["relevant_tools"] = ["evidence.value"]
    prepared = build_context(request, run, **options)
    data = payload(prepared)
    assert _POLICY[0] not in prepared.body()["messages"][0]["content"]
    if phase == "semantic_intake":
        assert list(data["ACTION_PARAMETERS"]) == ["normalize_request"]
    elif phase == "final":
        assert list(data["ACTION_PARAMETERS"]) == ["stop"]
    else:
        assert "call_tool" in data["ACTION_PARAMETERS"]
        assert "clarify" in data["ACTION_PARAMETERS"]


def test_registration_scope_can_stop_without_a_question_or_false_scientific_completion(tmp_path):
    store, run, request = _run(tmp_path)
    before_request = store.load_request(run).model_dump_json()
    before_permission = run.permission.model_dump_json()
    transport = ScriptedTransport({"action": "stop", "parameters": {},
        "reason": "Requirements registered; no calculation authorized or attempted this round. "
                  "Requested unit unknown; no energy value or scientific completion is claimed."})
    stopped = agent.execute(store, Config(), run.id, transport=transport)
    assert len(transport.sent) == stopped.usage.model_calls == 1
    assert stopped.state == "failed"  # Science is still unevidenced, despite completed registration.
    assert stopped.goal_status == {"water_energy": "insufficient_evidence"}
    assert stopped.decisions[-1]["action"] == "stop"
    assert store.active_clarification(stopped) is None
    assert not stopped.attempts and not stopped.calls and not stopped.result_ids
    assert stopped.usage.orca_starts_actual == stopped.usage.orca_starts_reserved == 0
    assert store.load_request(stopped).model_dump_json() == before_request
    assert stopped.permission.model_dump_json() == before_permission
    assert not build_report(store, stopped)["user_goal_complete"]
    assert request.goals[0].minimum_evidence == ["converged_scf@1"]


@pytest.mark.parametrize("scope,questions,unresolved", [
    ("unknown_spin", ["请提供水的多重度，以完整登记电子态。"], ["field:multiplicity"]),
    ("permission_missing", ["你要求启动计算；当前执行权限关闭，是否授权计算？"], ["execution_permission"]),
])
def test_no_execution_permission_does_not_suppress_a_needed_clarification(tmp_path, scope, questions, unresolved):
    store, run, _ = _run(tmp_path, scope)
    before_request = store.load_request(run).model_dump_json()
    before_permission = run.permission.model_dump_json()
    transport = ScriptedTransport({"action": "clarify", "parameters": {
        "questions": questions, "unresolved": unresolved}})
    waiting = agent.execute(store, Config(), run.id, transport=transport)
    assert len(transport.sent) == waiting.usage.model_calls == 1
    assert waiting.state == "waiting_user"
    assert store.active_clarification(waiting)["questions"] == questions
    assert store.active_clarification(waiting)["unresolved"] == unresolved
    assert store.load_request(waiting).model_dump_json() == before_request
    assert waiting.permission.model_dump_json() == before_permission
    assert not waiting.calls and not waiting.attempts and not waiting.result_ids
    assert waiting.usage.orca_starts_actual == waiting.usage.orca_starts_reserved == 0
    # The question persists without repeatedly reserving a model request.
    repeated = agent.execute(store, Config(), run.id, transport=ScriptedTransport(), resume=True)
    assert repeated.state == "waiting_user" and repeated.usage == waiting.usage


def test_real_v5_post_normalization_context_keeps_explicit_scope_and_unknown_unit_without_mutation():
    """Read-only failed-input replay; a passing test does not repair its model reply."""
    root = _PROJECT / "data/phase-b/reference"
    run_id = "run_6ff5f4f011cc4027bae5ef0aefdfeb39"
    directory = root / "runs" / run_id
    if not (directory / "run.json").is_file():
        pytest.skip("retained v5 real request absent; real-input replay unverified")
    hashes = {path: sha256_file(path) for path in directory.rglob("*.json")}
    store = Store(root)
    run = store.load_run(run_id)
    record = run.model_records[1]
    assert record["id"] == "model_117bc3ad6ec64b9484211e12d7e34c74"
    body = json.loads((directory / "model" / (record["id"] + ".request.json")).read_text(encoding="utf-8"))
    old = payload(SimpleNamespace(body=lambda: body))
    request = store.load_request_revision(run, old["AUTHORITY"]["basis"]["request_version"])
    assert request.normalization_status == "normalized" and not request.unresolved
    assert request.original_text == _REGISTRATION_TEXT
    assert not request.goals[0].conditions.get("unit")
    run.usage.model_calls = old["AUTHORITY"]["cumulative_usage"]["model_calls"]
    run.usage.model_tokens_used = old["AUTHORITY"]["cumulative_usage"]["model_tokens_used"]
    before = request.model_dump_json(), run.model_dump_json()
    prepared = build_context(request, run, user_messages=store.read_control(run.id)["messages"],
                             now=run.created_at + timedelta(seconds=30))
    rebuilt = payload(prepared)
    assert prepared.input_token_bound <= 12000
    assert all(part in prepared.body()["messages"][0]["content"] for part in _POLICY)
    assert set(rebuilt["ACTION_PARAMETERS"]) == {"clarify", "stop"}
    assert rebuilt["AUTHORITY"]["request"] == old["AUTHORITY"]["request"]
    assert rebuilt["AUTHORITY"]["user_originals"] == old["AUTHORITY"]["user_originals"]
    assert rebuilt["AUTHORITY"]["user_messages"] == old["AUTHORITY"]["user_messages"]
    assert rebuilt["AUTHORITY"]["permission"] == old["AUTHORITY"]["permission"]
    assert rebuilt["AUTHORITY"]["goal_status"] == {"goal_energy": "insufficient_evidence"}
    assert not rebuilt["TOOL_CATALOG"] and not rebuilt["DATA"]["results"]
    assert (request.model_dump_json(), run.model_dump_json()) == before
    assert {path: sha256_file(path) for path in hashes} == hashes
