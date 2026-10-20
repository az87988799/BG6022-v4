"""Direct refreeze-4 failures, read-only archived evidence; no live execution."""

import copy
import hashlib
import json
from datetime import timedelta
from pathlib import Path

import pytest
from test_context import objects
from test_decision_purpose_capacity import decoded
from test_general_preparation_admission import resolved
from test_rejected_proposal_context import correction_feedback
from test_simplified_semantic_selection import choices, intake, normal, submit
from test_structure_tools import mock_generator, tool_call

from orca_agent.condition_selection import commit_selections
from orca_agent.context import _rejected_proposal_view, build_context
from orca_agent.decision_purpose import relevant_result_ids
from orca_agent.delivery import collect_delivery_snapshot
from orca_agent.goals import current_goal_evidence, goal_evidence_assessment
from orca_agent.input_bindings import validate_input_result
from orca_agent.model_usage import current_basis
from orca_agent.models import Result
from orca_agent.store import Store
from orca_agent.tools import structure

PACK = Path(__file__).parents[2] / "docs/acceptance/code-as-tools-step7-refreeze-4/evidence"
RUNS = {"T1": "run_434d7dd084ee49139a0b37668df6608c",
        "T5": "run_ac956b8a55f8416b8914b841b2dbed15"}


def archived(task):
    store = object.__new__(Store)  # No initialization or writes to original evidence.
    store.root = (PACK / task / "data").resolve()
    run = store.load_run(RUNS[task])
    return store, run, store.load_request(run), store.load_plan(run) if run.plan_id else None


@pytest.mark.parametrize("task", ["T1", "T5"])
def test_real_selected_inputs_remain_applicable_visible_and_hash_bound(task):
    store, run, request, plan = archived(task)
    original = copy.deepcopy((run, request, plan))
    raw_run = store.path(f"runs/{run.id}/run.json").read_bytes()
    snapshot = collect_delivery_snapshot(store, run, request, plan)
    selected = relevant_result_ids(run, plan, snapshot=snapshot)
    results = [store.load_result(run.id, identifier) for identifier in selected]
    prepared = build_context(request, run, plan, results=results, delivery_snapshot=snapshot,
        now=run.created_at + timedelta(seconds=30),
        action_parameters={"initial_plan": {}, "stop": {}})
    wire = decoded(prepared)
    rows = wire["AUTHORITY"]["current_input_bindings"]
    for system_id, ports in run.input_bindings.items():
        for port, binding in ports.items():
            assert binding["result_id"] in selected
            result = store.load_result(run.id, binding["result_id"])
            assert validate_input_result(store, run, request, result, port) == binding
            row = next(item for item in rows if item["system_id"] == system_id and item["port"] == port)
            assert (row["artifact_id"], row["sha256"]) == (binding["artifact_id"], binding["sha256"])
            assert hashlib.sha256(store.artifact_path(row["artifact_id"]).read_bytes()).hexdigest() == row["sha256"]
            provenance = {key: result.source[key] for key in (
                "generator", "random_seed", "generation_reproducibility", "guarantee")
                if key in result.source}
            if provenance:
                assert wire["DATA"]["input_provenance"][result.id] == provenance
                assert "input_provenance" not in wire["AUTHORITY"]
                assert set(wire["DATA"]["input_provenance"][result.id]) == set(provenance)
                if port == "prepared_geometry":
                    assert provenance["random_seed"] is None
                    assert provenance["generation_reproducibility"] == "no seed exposed; reuse this immutable XYZ"
            for goal in request.goals:
                if goal.port == port:
                    assert goal_evidence_assessment(store, run, request, goal, result)["status"] == "passed"
                    assert current_goal_evidence(store, run, request, goal, plan)["assessment"]["status"] == "passed"
    assert prepared.input_token_bound <= run.configuration.model.input_tokens
    assert (run, request, plan) == original
    assert store.path(f"runs/{run.id}/run.json").read_bytes() == raw_run
    assert not any(row["goal_complete"] for row in snapshot["goals"]
                   if row["goal_id"] not in {goal.id for goal in request.goals
                       if goal.port in {"resolved_identity", "prepared_geometry"}})


@pytest.mark.parametrize("mutation", ["hash", "system", "result", "request", "selection"])
def test_bad_current_binding_cannot_supply_a_goal_route(mutation):
    store, run, request, _ = archived("T5")
    goal = next(goal for goal in request.goals if goal.port == "prepared_geometry")
    binding = run.input_bindings[goal.system_ids[0]][goal.port]
    if mutation == "selection":
        run.selected_results.clear()
    else:
        key, value = {"hash": ("sha256", "0" * 64), "system": ("system_id", "other"),
                      "result": ("result_id", "not_registered"),
                      "request": ("request_version", request.version - 1)}[mutation]
        binding[key] = value
    result = current_goal_evidence(store, run, request, goal)
    assert result["assessment"] is None or result["assessment"]["status"] != "passed"


def test_condition_selection_preserves_input_goal_status_without_changing_original(tmp_path, monkeypatch):
    store, config, run = intake(tmp_path, "请为乙醇生成初始几何并计算极化率。选择方法和基组。")
    parameters = normal(store, config, run)
    parameters["goals"].append({**parameters["goals"][0], "key": "geometry",
        "port": "prepared_geometry", "quantity": "初始几何"})
    run = submit(store, run, parameters)
    _, _, consumption = resolved(store, run, monkeypatch)
    xyz = "9\nfixture only\nC 0 0 0\nC 1.5 0 0\nO 2.9 0 0\nH -.5 .9 0\nH -.5 -.9 0\nH 0 0 1\nH 1.5 1 0\nH 1.5 -1 0\nH 3.3 .85 0\n"
    mock_generator(monkeypatch, xyz=xyz)
    call = tool_call(store, run, "structure.prepare",
        {"system_id": "system_target", "charge": 0, "multiplicity": 1}, consumption)
    data = structure.prepare(store, run, call)
    result = Result(run_id=run.id, call_id=call.id, step_id=call.step_id,
        operation_status=data["operation_status"], qualified_outputs=data["qualified_outputs"],
        checks={"prepared_geometry": data["qualified_outputs"]["prepared_geometry"]["checks"]},
        artifact_ids=data["artifact_ids"], source={"tool": call.tool, **data["source"]})
    store.finish_call(run, call, result)
    original = store.load_request(run)
    raw = store.path(f"runs/{run.id}/results/{result.id}.json").read_bytes()
    updated = commit_selections(store, run, choices(run), decision_id="choose", basis=current_basis(store, run))
    request = store.load_request(updated)
    geometry = next(goal for goal in request.goals if goal.port == "prepared_geometry")
    assert updated.goal_status[geometry.id] == "satisfied"
    assert updated.goal_evidence[geometry.id].result_id == result.id
    assert request.systems == original.systems and request.systems[0].geometry_artifact_id is None
    assert store.path(f"runs/{run.id}/results/{result.id}.json").read_bytes() == raw
    wrong = request.model_copy(deep=True)
    wrong.charge = 1
    assert goal_evidence_assessment(store, updated, wrong, geometry, result)["status"] == "unresolved"


@pytest.mark.parametrize("model_id", ["model_340d87b4cee3454d95406cf48408c4ef",
                                     "model_3cd8e8896e9a4c0f9322f04111418126"])
def test_real_h2_rejected_plan_keeps_complete_reference_structure(model_id):
    response_path = (PACK / "H2/data/runs/run_c570f932acb447b7b3ad79bc4c73d92d/model"
                     / f"{model_id}.response.json")
    proposal = json.loads(response_path.read_bytes())["proposal"]
    _, run = objects()
    feedback, basis = correction_feedback(run, proposal)
    view = _rejected_proposal_view(feedback, run, basis, limit=16384)
    assert view["projection"]["complete"] is False
    steps = view["proposal"]["parameters"]["steps"]
    assert len(steps) == len(proposal["parameters"]["steps"])
    assert steps[1]["inputs"] == proposal["parameters"]["steps"][1]["inputs"]
    assert view["proposal"]["parameters"]["goal_map"] == proposal["parameters"]["goal_map"]
    for source, projected in zip(proposal["parameters"]["steps"], steps, strict=True):
        assert projected["key"] == source["key"] and projected["tool"] == source["tool"]
        if source["tool"] == "python.execute":
            assert projected["parameters"]["code"] == source["parameters"]["code"]
    assert len(json.dumps(view["proposal"], ensure_ascii=False).encode()) < 16384
    # Exercise the actual compact planning context, which formerly replaced
    # this structure with a single omission marker at the 1 KiB preview tier.
    store = object.__new__(Store)
    store.root = (PACK / "H2/data").resolve()
    real_run = store.load_run("run_c570f932acb447b7b3ad79bc4c73d92d")
    request = store.load_request(real_run)
    record = next(item for item in real_run.model_records if item["id"] == model_id)
    decision = next(item for item in real_run.decisions if item["id"] == model_id)
    feedback = {"new_result_ids": [real_run.calls[0].result_id], "pending_step_ids": [],
        "validation_error": {"category": decision["parameters"]["error_category"],
                             "requirement": decision["parameters"]["requirement"]},
        "rejected_proposal": {"model_id": model_id, "proposal": proposal,
            "logical_request_id": record["logical_id"].rsplit("_", 1)[0],
            **{key: record[key] for key in ("basis", "request_hash", "response_record_sha256")}}}
    snapshot = collect_delivery_snapshot(store, real_run, request)
    results = [store.load_result(real_run.id, identifier)
               for identifier in relevant_result_ids(real_run, snapshot=snapshot)]
    prepared = build_context(request, real_run, results=results, delivery_snapshot=snapshot,
        feedback=feedback, now=real_run.created_at + timedelta(seconds=60),
        action_parameters={"initial_plan": {}, "call_tool": {}, "stop": {}})
    projected = decoded(prepared)["DATA"]["rejected_proposal"]["proposal"]
    assert projected["parameters"]["steps"][1]["inputs"] == proposal["parameters"]["steps"][1]["inputs"]
    assert projected["parameters"]["goal_map"] == proposal["parameters"]["goal_map"]
    assert prepared.input_token_bound <= real_run.configuration.model.input_tokens
