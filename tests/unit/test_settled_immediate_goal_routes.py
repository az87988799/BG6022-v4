"""Read-only a014 evidence replay; plans are materialized only in a temporary Store."""

import hashlib
import json
import shutil
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

import pytest

from orca_agent.goals import current_goal_evidence
from orca_agent.model_usage import current_basis
from orca_agent.models import Plan, fingerprint
from orca_agent.proposals import ProposalError, materialize_plan, settled_immediate_goal_binding
from orca_agent.store import Store

RUN_ID = "run_c13066f0a8dd4da4875d8321fee63b7e"
MODEL_ID = "model_f9093bd068064a58a24332880f3177d1"
GOAL_ID = "goal_prepared_geometry"
RESULT_ID = "result_0e321497c3a94856961a09da81a15f37"
XYZ_ID = "artifact_4cf0b357d7a541749376119c1b3baff0"
XYZ_SHA256 = "1d4a7b75058f9f07aab29c88aa1fca7a5b0f33ce46d1b402f787468e066ddca7"


@pytest.fixture(scope="module")
def a014_sources():
    root = Path(__file__).parents[2] / "data/live-repair/attempts/a014/data"
    run_path = root / "runs" / RUN_ID
    if not (run_path / "run.json").is_file():
        pytest.skip("local a014 prepared-input archive is not available")
    store = Store(root)
    run = store.load_run(RUN_ID)
    assert run.request_version == 3 and run.plan_id is None
    assert not run.attempts and run.usage.orca_starts_actual == 0
    response = run_path / "model" / f"{MODEL_ID}.response.json"
    record = next(item for item in run.model_records if item["id"] == MODEL_ID)
    assert hashlib.sha256(response.read_bytes()).hexdigest() == record["response_record_sha256"]
    proposal = json.loads(response.read_bytes())["proposal"]["parameters"]
    assert proposal["goal_map"][GOAL_ID] == {"port": "prepared_geometry", "step_key": "write_xyz"}
    paths = set(run_path.glob("*.json")) | {response}
    for directory in ("request-revisions", "plan-revisions", "decisions", "results"):
        paths.update((run_path / directory).glob("*.json"))
    for result_id in run.result_ids:
        for artifact_id in store.load_result(run.id, result_id).artifact_ids:
            paths.update(path for path in (root / "artifacts" / artifact_id).rglob("*") if path.is_file())
    assert hashlib.sha256(store.artifact_path(XYZ_ID).read_bytes()).hexdigest() == XYZ_SHA256
    hashes = {path: hashlib.sha256(path.read_bytes()).hexdigest() for path in paths}
    yield SimpleNamespace(root=root, paths=paths, proposal=proposal)
    assert {path: hashlib.sha256(path.read_bytes()).hexdigest() for path in paths} == hashes


@pytest.fixture
def replay(tmp_path, a014_sources):
    root = tmp_path / "a014_offline"
    for path in a014_sources.paths:
        target = root / path.relative_to(a014_sources.root)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, target)
    store = Store(root, environment_root=tmp_path / "environment")
    run = store.load_run(RUN_ID)
    request = store.load_request(run)
    goal = next(item for item in request.goals if item.id == GOAL_ID)
    # Retain the real cumulative usage, including its exhausted model budget.
    # This exercises plan construction, never runtime permission to execute it.
    return SimpleNamespace(store=store, run=run, request=request, goal=goal,
        proposal=deepcopy(a014_sources.proposal))


def prepared_audit(audits):
    records = [item for item in audits if item["goal_id"] == GOAL_ID]
    assert len(records) == 1
    audit = records[0]
    assert audit["rule"] == "settled-immediate-goal-binding-1"
    assert audit["request_version"] == 3
    assert audit["binding"]["port"] == "prepared_geometry"
    assert audit["binding"]["evidence"]["result_id"] == RESULT_ID
    assert audit["binding"]["evidence"]["rule_version"] == "structure-prepare-3"
    return audit


def test_actual_a014_proposal_passes_prepared_route_and_preserves_later_error(replay):
    case = replay
    before = deepcopy(case.proposal)
    audits = []
    # The complete historical proposal also misroutes delivery_bundle. Fixing
    # prepared_geometry must not implicitly approve that independent mismatch.
    with pytest.raises(ProposalError) as caught:
        materialize_plan(case.store, case.run, case.proposal, binding_canonicalizations=audits)
    assert caught.value.detail["code"] == "goal_output_route_mismatch"
    assert caught.value.detail["path"][2] == "goal_delivery"
    assert prepared_audit(audits)["original_route"] == {
        "kind": "future", "step_key": "write_xyz", "port": "prepared_geometry"}
    assert case.proposal == before


@pytest.mark.parametrize("route", ["wrong_producer", "gap", "omitted"])
def test_current_prepared_goal_uses_verified_evidence_in_materialized_plan(replay, route):
    case = replay
    writer = next(item for item in case.proposal["steps"] if item["key"] == "write_xyz")
    proposal = {"steps": [writer], "goal_map": {
        goal.id: {"port": goal.port, "gap": "Outside this evidence-route unit test."}
        for goal in case.request.goals}}
    if route == "wrong_producer":
        proposal["goal_map"][GOAL_ID] = case.proposal["goal_map"][GOAL_ID]
    elif route == "omitted":
        proposal["goal_map"].pop(GOAL_ID)
    before_run, before_request, before_proposal = deepcopy(case.run), deepcopy(case.request), deepcopy(proposal)
    audits = []
    plan = materialize_plan(case.store, case.run, proposal, binding_canonicalizations=audits)
    binding = plan.goal_map[GOAL_ID]
    assert binding.step_id is None and binding.gap is None
    assert binding.evidence.run_id == RUN_ID and binding.evidence.result_id == RESULT_ID
    assert binding.evidence.port == case.goal.port
    assert prepared_audit(audits)["original_route"]["kind"] == {
        "wrong_producer": "future", "gap": "gap", "omitted": "omitted"}[route]
    assert all(plan.goal_map[goal.id].gap for goal in case.request.goals if goal.id != GOAL_ID)
    assert case.run == before_run and case.request == before_request and proposal == before_proposal
    assert case.store.load_run(RUN_ID) == before_run and case.store.load_plan(case.run) is None
    assert hashlib.sha256(case.store.artifact_path(XYZ_ID).read_bytes()).hexdigest() == XYZ_SHA256


def test_canonicalization_audit_is_immutable_and_saved_commit_charges_once(replay):
    case = replay
    proposal = {"steps": [next(item for item in case.proposal["steps"] if item["key"] == "write_xyz")],
        "goal_map": {goal.id: {"port": goal.port, "gap": "Outside this offline commit test."}
                     for goal in case.request.goals}}
    proposal["goal_map"][GOAL_ID] = case.proposal["goal_map"][GOAL_ID]
    original_proposal, original_run = deepcopy(proposal), deepcopy(case.run)
    audits = []
    plan = materialize_plan(case.store, case.run, proposal, binding_canonicalizations=audits)
    decision_id = "offline_a014_settled_goal_commit"
    basis = current_basis(case.store, case.run)

    def interrupt_after_save(point):
        if point == "after_revision_saved":
            raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        case.store.commit_revision(case.run, plan, decision_id=decision_id, basis=basis,
            binding_canonicalizations=audits, fault=interrupt_after_save)
    assert case.store.load_run(RUN_ID) == original_run
    path = case.store.path(f"runs/{RUN_ID}/decisions/{decision_id}.json")
    saved_bytes = path.read_bytes()
    saved = json.loads(saved_bytes)
    assert saved["binding_canonicalizations"] == audits
    assert prepared_audit(saved["binding_canonicalizations"])["original_binding_sha256"] == fingerprint(
        original_proposal["goal_map"][GOAL_ID])
    assert saved["plan"] == plan.model_dump(mode="json")
    # Reconnect the exact saved candidate, as agent recovery does. No model
    # capacity or execution budget is added to the archived Run's temporary copy.
    resumed_plan = Plan.model_validate(saved["plan"])
    activated = case.store.commit_revision(case.store.load_run(RUN_ID), resumed_plan,
        decision_id=decision_id, basis=basis,
        binding_canonicalizations=saved["binding_canonicalizations"])
    summary = next(item for item in activated.decisions if item["id"] == decision_id)
    assert summary["record_sha256"] == fingerprint(saved)
    assert case.store.load_plan(activated) == resumed_plan
    assert activated.usage.plan_revisions == original_run.usage.plan_revisions + 1
    expected_usage = original_run.usage.model_dump(mode="json")
    expected_usage["plan_revisions"] += 1
    expected_usage["logical_steps"] = sorted(set(expected_usage["logical_steps"]) |
                                            {step.logical_id for step in plan.steps})
    assert activated.usage.model_dump(mode="json") == expected_usage
    duplicate = case.store.commit_revision(activated, resumed_plan, decision_id=decision_id,
        basis=basis, binding_canonicalizations=saved["binding_canonicalizations"])
    assert duplicate == activated
    assert path.read_bytes() == saved_bytes
    assert case.run == original_run and proposal == original_proposal


@pytest.mark.parametrize("barrier", ["failed_call", "wrong_system", "hash_mismatch", "new_request"])
def test_invalid_or_stale_prepared_input_is_not_a_settled_goal_route(replay, barrier):
    case = replay
    selected = current_goal_evidence(case.store, case.run, case.request, case.goal)
    assert settled_immediate_goal_binding(case.run, case.request, case.goal, selected)
    if barrier == "failed_call":
        next(call for call in case.run.calls if call.result_id == RESULT_ID).state = "failed"
    elif barrier == "wrong_system":
        case.goal.system_ids = ["different_system"]
    elif barrier == "hash_mismatch":
        # Deliberate corruption of the temporary copy only; source hashes are
        # rechecked by the module fixture after every test has completed.
        path = case.store.artifact_path(XYZ_ID)
        path.write_bytes(path.read_bytes() + b"\nchanged temporary copy\n")
    else:
        case.request.version += 1
        case.run.request_version = case.request.version
    selected = current_goal_evidence(case.store, case.run, case.request, case.goal)
    assert settled_immediate_goal_binding(case.run, case.request, case.goal, selected) is None

