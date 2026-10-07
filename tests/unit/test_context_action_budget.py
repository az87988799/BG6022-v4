"""Budget-aware action projection; all executions below use offline fixtures."""

import json
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from orca_agent.context import build_context
from orca_agent.model_usage import current_basis
from orca_agent.models import Goal, InputRef, OutputBinding, Plan, Result, Step
from orca_agent.proposals import materialize_plan
from orca_agent.semantic import action_parameters
from orca_agent.store import BudgetExceeded, Store, sha256_file
from orca_agent.tools.registry import get_tool
from tests.unit.test_agent import initial_proposal, make_run
from tests.unit.test_context import objects, payload


def _plan(request, run, tool="evidence.field"):
    values = {"geometry": InputRef(artifact_id="geometry1")} if tool == "orca.sp" else {}
    if tool == "evidence.field":
        values["parameters"] = {"artifact_id": "geometry1", "field": "energy"}
    elif tool == "evidence.import":
        values["parameters"] = {"source_id": "registered-source"}
    elif tool.startswith("analysis."):
        values["parameters"] = {"goal_id": request.goals[0].id}
    definition = get_tool(tool)
    request.goals[0].port = (definition.output_ports or definition.observation_outputs)[0]
    step = Step(id="ready", logical_id="ready", tool=tool, **values)
    plan = Plan(request_id=request.id, steps=[step],
                goal_map={request.goals[0].id: OutputBinding(step_id=step.id, port=request.goals[0].port)})
    run.plan_id, run.plan_version = plan.id, plan.version
    run.initial_science_steps = [step.logical_id] if tool == "orca.sp" else []
    return plan


def _actions(data):
    actions = set(data["PROPOSAL_SCHEMA"]["properties"]["action"]["enum"])
    if "ACTION_PARAMETERS" in data:
        assert set(data["ACTION_PARAMETERS"]) == actions
    else:
        assert actions == {"clarify", "stop"}
        assert {branch["properties"]["action"]["const"]
                for branch in data["PROPOSAL_SCHEMA"]["oneOf"]} == actions
    return actions


@pytest.mark.parametrize("has_plan", [False, True])
@pytest.mark.parametrize("used", [1, 2])
def test_revision_limit_filters_only_exhausted_creation_or_revision_and_plan_guidance(has_plan, used):
    request, run = objects(scientific=False)
    run.permission.allowed_tools = ["evidence.import"]
    run.permission.artifact_writes = True
    run.permission.source_ids = ["registered-source"]
    plan = _plan(request, run, "evidence.import") if has_plan else None
    # None Plan with a baseline represents a prior Plan removed by a Request
    # update; it must not regain a free initial Plan by changing its identity.
    run.initial_science_steps = []
    run.usage.plan_revisions = used
    before = request.model_dump_json(), run.model_dump_json()
    data = payload(build_context(request, run, plan))
    action = "revise_plan" if has_plan else "initial_plan"
    if used == run.budget.plan_revisions:
        assert _actions(data) == {"clarify", "stop"}
        assert "PLAN_RULES" not in data and "PLAN_REFERENCES" not in data
    else:
        assert _actions(data) == {action, "clarify", "stop"}
        assert "PLAN_RULES" in data and "PLAN_REFERENCES" in data
    authority = data["AUTHORITY"]
    assert authority["budget_limits"] == run.budget.model_dump(mode="json", exclude={
        "identity_queries", "structure_preparations"})
    assert run.budget.identity_queries == run.budget.structure_preparations == 0
    assert authority["cumulative_usage"]["plan_revisions"] == used
    assert authority["remaining"]["plan_revisions"] == 2 - used
    assert (request.model_dump_json(), run.model_dump_json()) == before


def test_zero_revision_allowance_still_allows_the_first_plan_as_store_requires(tmp_path):
    store, original, _ = make_run(tmp_path)
    request = store.load_request(original)
    run = store.create_run(request, None, original.permission,
                           original.budget.model_copy(update={"plan_revisions": 0}))
    before_request = request.model_dump_json()
    assert run.initial_science_steps is None
    data = payload(build_context(request, run))
    assert "initial_plan" in _actions(data) and "PLAN_RULES" in data
    plan = materialize_plan(store, run, initial_proposal(data)["parameters"])
    updated = store.commit_revision(run, plan, decision_id="offline_first_plan", basis=current_basis(store, run))
    assert updated.initial_science_steps == []
    assert updated.usage.plan_revisions == 0
    assert not updated.calls and not updated.attempts
    assert updated.usage.orca_starts_actual == 0
    assert store.load_request(updated).model_dump_json() == before_request
    next_data = payload(build_context(request, updated, store.load_plan(updated)))
    assert "revise_plan" not in _actions(next_data) and "PLAN_RULES" not in next_data


@pytest.mark.parametrize("tool", ["evidence.field", "analysis.energy_compare", "orca.sp"])
def test_exhausted_revisions_preserve_a_ready_step_in_an_existing_plan(tool):
    request, run = objects()
    run.permission.allowed_tools = [tool]
    run.permission.artifact_writes = True
    run.budget.orca_starts = 1
    run.budget.evidence_reads = 0 if tool != "evidence.field" else 1
    plan = _plan(request, run, tool)
    run.usage.plan_revisions = run.budget.plan_revisions
    before = request.model_dump_json(), run.model_dump_json(), plan.model_dump_json()
    data = payload(build_context(request, run, plan, feedback={"pending_step_ids": ["ready"]}))
    assert _actions(data) == {"call_tool", "clarify", "stop"}
    assert data["ACTION_PARAMETERS"]["call_tool"] == {"step_id": "ready"}
    assert "PLAN_RULES" not in data and "PLAN_REFERENCES" not in data
    assert data["AUTHORITY"]["plan"]["steps"][0]["tool"] == tool
    assert (request.model_dump_json(), run.model_dump_json(), plan.model_dump_json()) == before


@pytest.mark.parametrize("has_plan", [False, True])
def test_revision_exhaustion_keeps_a_legitimate_immediate_query(tmp_path, has_plan):
    store, original, artifact = make_run(tmp_path, initial_plan=has_plan)
    run = (store.load_run(original.id) if has_plan else store.create_run(
        store.load_request(original), None, original.permission,
        original.budget.model_copy(update={"plan_revisions": 0})))
    if has_plan:
        run.usage.plan_revisions = run.budget.plan_revisions
    store.save_run(run)
    request, plan = store.load_request(run), store.load_plan(run)
    before = run.model_dump_json()
    parameters = {"artifact_id": artifact.id, "path": [{"kind": "key", "key": "a"}]}
    data = payload(build_context(request, run, plan))
    assert _actions(data) == ({"call_tool", "clarify", "stop"} if has_plan else
                              {"initial_plan", "call_tool", "clarify", "stop"})
    assert data["ACTION_PARAMETERS"]["call_tool"] == {
        "tool": "<read-only Tool.name>", "parameters": {}}
    # The Store's real permission/cost gate agrees; validate_only never executes
    # or reserves a read, creates a Result, or calls a model.
    assert store.reserve_call(run, "evidence.value", parameters, validate_only=True) is None
    assert run.model_dump_json() == before


@pytest.mark.parametrize("has_plan", [False, True])
def test_exhausted_immediate_read_budget_removes_call_without_hiding_authority(has_plan):
    request, run = objects(scientific=False)
    run.permission.allowed_tools = ["evidence.field"]
    plan = _plan(request, run) if has_plan else None
    run.initial_science_steps = []
    run.usage.plan_revisions = run.budget.plan_revisions
    run.usage.evidence_reads = run.budget.evidence_reads
    request.conditions_source = {"basis": "explicit", "charge": "default"}
    request.unresolved = ["observation unit unknown"]
    result = Result(run_id=run.id, operation_status="completed", observations={
        "value_observation": {"value": 2, "units": None, "scientific_status": "unverified",
                              "conditions": "unknown", "sha256": "a" * 64}})
    run.result_ids = [result.id]
    before = request.model_dump_json(), run.model_dump_json(), result.model_dump_json()
    data = payload(build_context(request, run, plan, results=[result]))
    assert _actions(data) == {"clarify", "stop"}
    assert "PLAN_RULES" not in data
    authority = data["AUTHORITY"]
    assert authority["permission"] == run.permission.model_dump(mode="json", exclude={
        "external_identity_queries", "geometry_preparation"})
    assert not run.permission.external_identity_queries and not run.permission.geometry_preparation
    assert authority["budget_limits"] == run.budget.model_dump(mode="json", exclude={
        "identity_queries", "structure_preparations"})
    assert run.budget.identity_queries == run.budget.structure_preparations == 0
    assert authority["cumulative_usage"]["evidence_reads"] == run.budget.evidence_reads
    assert authority["request"]["conditions_source"] == request.conditions_source
    assert authority["request"]["conditions"] == request.conditions
    assert authority["request"]["unresolved"] == request.unresolved
    assert authority["request"]["goals"][0]["minimum_evidence"] == request.goals[0].minimum_evidence
    assert data["DATA"]["results"][0]["unqualified_observations"] == result.observations
    assert authority["related_results"] == [result.id]
    assert (request.model_dump_json(), run.model_dump_json(), result.model_dump_json()) == before


def test_pending_read_still_uses_store_cost_gate_when_its_read_budget_is_exhausted(tmp_path):
    store, run, _ = make_run(tmp_path, initial_plan=True)
    request, plan = store.load_request(run), store.load_plan(run)
    run.usage.evidence_reads = run.budget.evidence_reads
    run.usage.plan_revisions = run.budget.plan_revisions
    store.save_run(run)
    step = plan.steps[0]
    before = run.model_dump_json()
    data = payload(build_context(request, run, plan, feedback={"pending_step_ids": [step.id]}))
    # Projection only filters immediate reads here. A structural ready Step is
    # not a promise that all execution gates pass; the existing Store is final.
    assert data["ACTION_PARAMETERS"]["call_tool"] == {"step_id": step.id}
    with pytest.raises(BudgetExceeded, match="evidence_reads budget exhausted"):
        store.reserve_call(run, step.tool, step.parameters.model_dump(), step=step, validate_only=True)
    assert run.model_dump_json() == before and not run.calls


def test_budget_projection_does_not_remove_semantic_normalization():
    request, run = objects(scientific=False)
    request.normalization_status = "pending"
    run.initial_science_steps = []
    run.usage.plan_revisions = run.budget.plan_revisions
    run.usage.evidence_reads = run.budget.evidence_reads
    data = payload(build_context(request, run, action_parameters=action_parameters(request=request)))
    assert _actions(data) == {"normalize_request"}
    assert "PLAN_RULES" not in data and "PLAN_REFERENCES" not in data


def test_portable_insufficient_sampling_feedback_retains_all_five_members_at_revision_limit():
    request, run = objects(scientific=False)
    request.original_text = "Summarize five registered sampling members; no additional calculation."
    request.goals = [Goal(id="sampling", port="sampling", minimum_check_version="finite-sampling-1",
                         conditions={"members": [{"id": key} for key in "ABCDE"]})]
    run.permission.allowed_tools = ["analysis.finite_sampling"]
    run.permission.artifact_writes = True
    run.budget.orca_starts = run.budget.extra_orca_starts = 0
    run.budget.plan_revisions = 0
    plan = _plan(request, run, "analysis.finite_sampling")
    # Portable synthetic context evidence, never a qualified scientific Result.
    members = [{"member_id": key, "status": "missing", "reason": "no qualified energy source"}
               for key in "ABCDE"]
    result = Result(run_id=run.id, step_id=plan.steps[0].id, operation_status="completed",
                    observations={"analysis": {"members": members, "goal_satisfied": False,
                                  "reason": "insufficient_evidence", "additional_execution_allowed": False}})
    run.result_ids = [result.id]
    run.goal_status = {"sampling": "insufficient_evidence"}
    before = request.model_dump_json(), run.model_dump_json(), result.model_dump_json()
    prepared = build_context(request, run, plan, results=[result])
    data = payload(prepared)
    assert _actions(data) == {"clarify", "stop"}
    assert "PLAN_RULES" not in data
    assert data["DATA"]["results"][0]["unqualified_observations"]["analysis"]["members"] == members
    assert data["AUTHORITY"]["request"]["goals"][0]["conditions"] == request.goals[0].conditions
    assert data["AUTHORITY"]["goal_status"] == run.goal_status
    assert data["AUTHORITY"]["remaining"]["orca_starts"] == 0
    assert prepared.input_token_bound <= 12000
    assert (request.model_dump_json(), run.model_dump_json(), result.model_dump_json()) == before


@pytest.mark.parametrize("index", [0, 1, 2], ids=["first_plan", "analysis_feedback", "correction"])
def test_retained_v06_budget_actions_rebuild_without_changing_history(index):
    """Rebuild real v12 inputs locally; absence is a skip, never invented evidence."""
    root = Path(__file__).resolve().parents[2] / "data/phase-b/reference"
    run_id = "run_333aae7833ab445493ec77a72f6ee991"
    directory = root / "runs" / run_id
    if not (directory / "run.json").is_file():
        pytest.skip("retained V06 development archive absent; real-request rebuild unverified")
    before_files = {path: sha256_file(path) for path in directory.rglob("*.json")}
    store = Store(root)
    persisted = store.load_run(run_id)
    record = persisted.model_records[index]
    assert record["prompt_version"] == "agent-json-v12"
    original_body = json.loads((directory / "model" / (record["id"] + ".request.json")).read_text(encoding="utf-8"))
    original = payload(SimpleNamespace(body=lambda: original_body))
    authority = original["AUTHORITY"]
    run = persisted.model_copy(deep=True)
    request = store.load_request_revision(run, authority["basis"]["request_version"])
    plan_version = authority["basis"]["plan_version"]
    plan = store.load_plan_revision(run, plan_version) if plan_version is not None else None
    run.request_version = request.version
    run.plan_id, run.plan_version = (plan.id, plan.version) if plan else (None, None)
    if index == 0:
        run.initial_science_steps = None
    run.goal_status = authority["goal_status"]
    results = [store.load_result(run.id, item["result_id"]) for item in original["DATA"]["results"]]
    run.result_ids = [result.id for result in results]
    run.calls = [call for call in run.calls if call.result_id in run.result_ids]
    run.selected_results = {key: value for key, value in run.selected_results.items() if value in run.result_ids}
    for field in ("model_calls", "model_tokens_used", "model_tokens_unknown", "plan_revisions",
                  "decision_rounds", "evidence_reads", "analysis_executions"):
        setattr(run.usage, field, authority["cumulative_usage"].get(field, 0))
    if index == 0:
        run.usage.logical_steps = []
    before_objects = request.model_dump_json(), run.model_dump_json(), [item.model_dump_json() for item in results]
    prepared = build_context(request, run, plan, results=results,
        relevant_tools=run.permission.allowed_tools,
        feedback={**original["DATA"]["feedback"], **original["CONTROL"],
                  "new_result_ids": authority["related_results"],
                  "current_goal_use": original["DATA"].get("current_goal_use", [])},
        now=run.created_at + timedelta(seconds=30))
    rebuilt = payload(prepared)
    if index == 0:
        assert _actions(rebuilt) == {"initial_plan", "clarify", "stop"}
        assert "PLAN_RULES" in rebuilt
    else:
        assert _actions(rebuilt) == {"clarify", "stop"}
        assert "PLAN_RULES" not in rebuilt and "PLAN_REFERENCES" not in rebuilt
    assert prepared.input_token_bound <= 12000
    for field in ("budget_limits", "permission", "goal_status", "related_results", "request", "cumulative_usage"):
        assert rebuilt["AUTHORITY"][field] == authority[field]
    assert rebuilt.get("CONTROL", {}) == original["CONTROL"]
    # v13 additionally projects the actual settled Call's registry effects.
    # Every pre-existing data fact must survive, allowing that truthful addition.
    assert {key: value for key, value in rebuilt["DATA"].items() if key != "tool_effects"} == original["DATA"]
    if index:
        assert rebuilt["DATA"]["tool_effects"] == {
            "analysis.finite_sampling": get_tool("analysis.finite_sampling").effects}
    assert (request.model_dump_json(), run.model_dump_json(), [item.model_dump_json() for item in results]) == before_objects
    assert {path: sha256_file(path) for path in before_files} == before_files
