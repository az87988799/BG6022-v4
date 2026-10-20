"""Current archived files can lead to explicit analysis; no science is executed."""

import hashlib
import json
from copy import deepcopy
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from orca_agent import delivery, model_usage
from orca_agent.context import build_context
from orca_agent.delivery import collect_goal_selections, delivery_snapshot
from orca_agent.proposals import ProposalError
from orca_agent.store import Store
from orca_agent.terminal import validate_continuation_before_stop, validate_terminal_explanation
from tests.unit.test_context import payload
from tests.unit.test_terminal_contract import valid_parameters

RUN_ID = "run_1329f4207b8c447db60717d2e9a0ee06"
ATTEMPT_ID = "attempt_d74f7f20efc64f2aae14dd5f800c9112"
STDOUT_ID = "artifact_811626115f0b450d983b8bd9dcbc8ec3"
STDOUT_SHA256 = "1828f00696eab95b90f782d4f4cfb60dd6105f8c35540c82af1ef9a6e4652bcf"
ANALYSIS_KIND = "plan_analysis_from_current_evidence"
REJECTED_ANALYSIS_ID = "result_17353ee8959c473b8f399e68b04e9cfd"
REJECTION_DETAIL = "observation task source was not archived"


def file_hash(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.fixture(scope="module")
def archived_gap():
    root = Path(__file__).parents[2] / "data/live-repair/attempts/a011/data"
    if not (root / "runs" / RUN_ID / "run.json").is_file():
        pytest.skip("local a011 native evidence archive is not available")
    store = Store(root)
    run = store.load_run(RUN_ID)
    request, plan = store.load_request_revision(run, 3), store.load_plan_revision(run, 2)
    assert request.version == 3 and plan.version == 2
    # Replay the approved gap Plan in memory. A live coordinator may already
    # have added the later analysis Plan; never change or save its actual Run.
    # Preserve cumulative usage conservatively rather than refund later work.
    run.request_version, run.plan_id, run.plan_version = 3, plan.id, 2
    run.attempts = [item for item in run.attempts if item.id == ATTEMPT_ID]
    run.calls = [item for item in run.calls if item.plan_version is None or item.plan_version <= 2]
    retained = {item.result_id for item in [*run.attempts, *run.calls] if item.result_id}
    run.result_ids = [identifier for identifier in run.result_ids if identifier in retained]
    run.selected_results = {step: result for step, result in run.selected_results.items() if result in retained}
    run.internal_calls = [item for item in run.internal_calls if item.parent_attempt_id == ATTEMPT_ID]
    attempt = next(item for item in run.attempts if item.id == ATTEMPT_ID)
    result = store.load_result(run.id, attempt.result_id)
    assert attempt.state == result.operation_status == "completed"
    assert result.observations["evidence_collection_complete"] is True
    assert not result.observation_records and not run.internal_calls
    goals = [goal for goal in request.goals if goal.evidence_contract_version == "generic-observation-1"]
    assert len(goals) == 3 and all(plan.goal_map[goal.id].gap for goal in goals)
    for goal in goals:
        run.goal_evidence.pop(goal.id, None)
        run.goal_status[goal.id] = "insufficient_evidence"
    # The coordinator may still settle model calls in run.json. These selected
    # source files and their metadata are immutable even while the Run is active.
    originals = {root / "runs" / run.id / "results" / f"{result.id}.json"}
    originals.add(root / attempt.directory / "intent.json")
    identifiers = set(result.artifact_ids)
    identifiers.add(attempt.frozen_step.parameters.input.artifact_id)
    for identifier in identifiers:
        originals.add(root / "artifacts" / identifier / "artifact.json")
        originals.add(store.artifact_path(identifier))
    hashes = {path: file_hash(path) for path in originals}
    yield SimpleNamespace(store=store, request=request, run=run, plan=plan,
                          result=result, goal_ids=[goal.id for goal in goals], hashes=hashes)
    assert {path: file_hash(path) for path in originals} == hashes


@pytest.fixture
def gap_case(archived_gap):
    return SimpleNamespace(store=archived_gap.store,
        request=deepcopy(archived_gap.request), run=deepcopy(archived_gap.run),
        plan=deepcopy(archived_gap.plan), result=deepcopy(archived_gap.result),
        goal_ids=archived_gap.goal_ids)


def snapshot(case):
    selected = collect_goal_selections(case.store, case.run, case.request, case.plan)
    return selected, delivery_snapshot(case.request, case.run, selected, plan=case.plan)


def analysis_work(value):
    return [value["references"][item["value"]["work_ref"]]
            for item in value["facts"]
            if item["kind"] == "current_plan_work" and item["value"]["kind"] == ANALYSIS_KIND]


def test_current_native_files_plan_analysis_without_changing_goal_or_plan(gap_case):
    case = gap_case
    before = deepcopy((case.request, case.run, case.plan))
    selections, value = snapshot(case)
    for identifier in case.goal_ids:
        selection = selections[identifier]
        assert selection["result"] is None and selection["assessment"] is None
        assert selection["binding"].gap
        candidates = selection["analysis_source_candidates"]
        assert candidates and STDOUT_ID in json.dumps(candidates)
        assert STDOUT_SHA256 in json.dumps(candidates)
    work = analysis_work(value)
    assert len(work) == 3
    assert all(item["new_orca_starts_required"] == 0 for item in work)
    assert all(item["source_candidates"] for item in work)
    assert all(not goal["goal_complete"] for goal in value["goals"] if goal["goal_id"] in case.goal_ids)
    assert len([action for action in value["next_actions"] if action["kind"] == ANALYSIS_KIND]) == 3
    assert (case.request, case.run, case.plan) == before


def test_existing_files_need_analysis_permission_but_no_new_orca_allowance(gap_case):
    case = gap_case
    case.run.permission.scientific_execution = case.run.permission.allow_additional_science = False
    case.run.budget.orca_starts = case.run.budget.extra_orca_starts = 0
    case.run.budget.internal_work_units = 0
    _, value = snapshot(case)
    assert len(analysis_work(value)) == 3
    assert all(work["new_orca_starts_required"] == 0 for work in analysis_work(value))


@pytest.mark.parametrize("invalid", ["request", "system", "foreign", "hash", "unknown", "qualified"])
def test_gap_source_must_remain_current_owned_readable_and_unqualified(gap_case, monkeypatch, invalid):
    case = gap_case
    attempt = next(item for item in case.run.attempts if item.id == ATTEMPT_ID)
    if invalid == "request":
        attempt.request_version -= 1
    elif invalid == "system":
        for goal in case.request.goals:
            if goal.id in case.goal_ids:
                goal.system_ids = ["another_system"]
    elif invalid == "foreign":
        original = case.store.load_artifact

        def foreign(identifier):
            artifact = original(identifier)
            return artifact.model_copy(update={"run_id": "foreign_run"}) if identifier == STDOUT_ID else artifact

        monkeypatch.setattr(case.store, "load_artifact", foreign)
    elif invalid == "hash":
        original = case.store.artifact_path

        def unreadable(identifier, **kwargs):
            if identifier == STDOUT_ID:
                raise ValueError("archived source hash changed")
            return original(identifier, **kwargs)

        monkeypatch.setattr(case.store, "artifact_path", unreadable)
    elif invalid == "unknown":
        attempt.state, attempt.finished_at = "unknown", None
    else:
        for goal in case.request.goals:
            if goal.id in case.goal_ids:
                goal.verification = "qualified"
    selections, value = snapshot(case)
    assert not analysis_work(value)
    if invalid != "unknown":
        assert all(not selections[identifier].get("analysis_source_candidates") for identifier in case.goal_ids)


@pytest.mark.parametrize("barrier", ["analysis", "write", "revision", "permission"])
def test_archived_candidates_do_not_force_analysis_without_necessary_allowance(gap_case, barrier):
    case = gap_case
    if barrier == "permission":
        case.run.permission.code_execution = False
    else:
        field = {"analysis": "analysis_executions", "write": "file_writes", "revision": "plan_revisions"}[barrier]
        setattr(case.run.budget, field, getattr(case.run.usage, field))
    _, value = snapshot(case)
    assert not analysis_work(value)


def test_context_preserves_gap_analysis_route_and_raw_source_hash(gap_case):
    case = gap_case
    _, value = snapshot(case)
    results = [case.store.load_result(case.run.id, identifier)
               for identifier in case.run.selected_results.values()]
    prepared = build_context(case.request, case.run, case.plan, results=results,
        delivery_snapshot=value, feedback={"new_result_ids": [case.result.id],
            "pending_step_ids": [], "delivery_snapshot": value},
        relevant_tools=case.run.permission.allowed_tools,
        now=case.run.created_at + timedelta(minutes=6), model_profile="thinking_low")
    wire = payload(prepared)
    data = json.dumps(wire["DATA"], ensure_ascii=False)
    assert ANALYSIS_KIND in data and STDOUT_SHA256 in data
    action = next(item for item in value["next_actions"] if item["kind"] == ANALYSIS_KIND)
    assert action["text"] in data
    assert prepared.input_token_bound <= case.run.budget.input_tokens


def test_gap_analysis_work_is_required_by_explanation_and_stop_gate(gap_case, monkeypatch):
    case = gap_case
    _, value = snapshot(case)
    work = [item for item in value["facts"]
            if item["kind"] == "current_plan_work" and item["value"]["kind"] == ANALYSIS_KIND]
    assert work
    parameters = deepcopy(valid_parameters(value))
    assert validate_terminal_explanation(parameters, value)
    explanation = next(item for item in parameters["delivery"]["goal_explanations"]
                       if item["goal_ref"] == work[0]["goal_ref"])
    explanation["fact_refs"].remove(work[0]["ref"])
    with pytest.raises(ProposalError) as caught:
        validate_terminal_explanation(parameters, value)
    assert caught.value.detail["code"] == "terminal_fact_scope"

    # Isolate the gate contract from the still-active archive's model ledger.
    # This stub is not evidence that the live Run retains planning capacity.
    def planning_margin(request, run, *, purpose):
        assert request is case.request and run is case.run and purpose.kind == "planning"
        return {"can_send_before_final": True}

    monkeypatch.setattr(model_usage, "final_explanation_budget", planning_margin)
    with pytest.raises(ProposalError) as caught:
        validate_continuation_before_stop(case.request, case.run, value)
    assert caught.value.detail["code"] == "terminal_current_work_remaining"
    assert {item["ref"] for item in work} <= set(caught.value.detail["required_fact_refs"])
    expected_actions = {item["ref"] for item in value["next_actions"] if item["kind"] == ANALYSIS_KIND}
    assert expected_actions and expected_actions <= set(caught.value.detail["next_action_refs"])


@pytest.fixture(scope="module")
def archived_bound_rejection(archived_gap):
    store = archived_gap.store
    run = store.load_run(RUN_ID)
    request, plan = store.load_request_revision(run, 3), store.load_plan_revision(run, 3)
    # Read the actual completed Plan 3 analysis and its native source, even if
    # the coordinator has since tried another repair. This view is never saved.
    run.request_version, run.plan_id, run.plan_version = 3, plan.id, 3
    run.attempts = [item for item in run.attempts
                    if item.id == ATTEMPT_ID or item.result_id == REJECTED_ANALYSIS_ID]
    assert len(run.attempts) == 2 and all(item.state == "completed" for item in run.attempts)
    run.calls = [item for item in run.calls if item.plan_version is None or item.plan_version <= 3]
    retained = {item.result_id for item in [*run.attempts, *run.calls] if item.result_id}
    run.result_ids = [identifier for identifier in run.result_ids if identifier in retained]
    run.selected_results = {item.step_id: item.result_id for item in [*run.attempts, *run.calls]
                            if item.result_id}
    run.internal_calls = [item for item in run.internal_calls
                          if item.parent_attempt_id in {attempt.id for attempt in run.attempts}]
    assert not run.internal_calls
    result = store.load_result(run.id, REJECTED_ANALYSIS_ID)
    assert result.operation_status == "completed" and not result.observation_records
    goals = [goal for goal in request.goals if goal.evidence_contract_version == "generic-observation-1"]
    assert len(goals) == 3 and all(plan.goal_map[goal.id].step_id == result.step_id for goal in goals)
    for goal in goals:
        run.goal_evidence.pop(goal.id, None)
        run.goal_status[goal.id] = "insufficient_evidence"
    root = Path(__file__).parents[2] / "data/live-repair/attempts/a011/data"
    originals = set()
    for attempt in run.attempts:
        source = store.load_result(run.id, attempt.result_id)
        originals.add(root / "runs" / run.id / "results" / f"{source.id}.json")
        originals.add(root / attempt.directory / "intent.json")
        code = (getattr(attempt.frozen_step.parameters, "code", None)
                or getattr(attempt.frozen_step.parameters, "input", None))
        for identifier in {*source.artifact_ids, code.artifact_id}:
            originals.add(root / "artifacts" / identifier / "artifact.json")
            originals.add(store.artifact_path(identifier))
    hashes = {path: file_hash(path) for path in originals}
    yield SimpleNamespace(store=store, request=request, run=run, plan=plan,
                          result=result, goal_ids=[goal.id for goal in goals])
    assert {path: file_hash(path) for path in originals} == hashes


@pytest.fixture
def bound_rejection_case(archived_bound_rejection):
    case = archived_bound_rejection
    return SimpleNamespace(store=case.store, request=deepcopy(case.request),
        run=deepcopy(case.run), plan=deepcopy(case.plan), result=deepcopy(case.result),
        goal_ids=case.goal_ids)


def test_bound_manifest_rejection_reuses_current_files_without_orca(bound_rejection_case, record_property):
    case = bound_rejection_case
    before = deepcopy((case.request, case.run, case.plan))
    selections, value = snapshot(case)
    for identifier in case.goal_ids:
        selected = selections[identifier]
        assert selected["result"] == case.result
        # The repaired task-source reader can now parse the manifest, but must
        # not upgrade the old Result whose archived admitted records are empty.
        assert selected["assessment"]["reasons"] == ["generic_observation_differs_from_archived_manifest"]
        assert any(item.get("name") == selected["binding"].port and item.get("detail") == REJECTION_DETAIL
                   for item in selected["result"].diagnostics)
        assert STDOUT_SHA256 in json.dumps(selected["analysis_source_candidates"])
    work = analysis_work(value)
    assert len(work) == 3
    assert all(item["new_orca_starts_required"] == 0 for item in work)
    assert all(item["rejected_result_id"] == REJECTED_ANALYSIS_ID for item in work)
    assert all(any(entry.get("detail") == REJECTION_DETAIL for entry in item["admission_diagnostics"])
               for item in work)
    assert len([item for item in value["next_actions"] if item["kind"] == ANALYSIS_KIND]) == 3
    assert not any(item["goal_complete"] for item in value["goals"] if item["goal_id"] in case.goal_ids)
    results = [case.store.load_result(case.run.id, identifier)
               for identifier in case.run.selected_results.values()]
    # Synthetic model capacity isolates rendering from the active coordinator's
    # exhausted ledger. It neither changes science budgets nor asserts that the
    # live Run can continue; the original case remains unchanged and unsaved.
    context_run = deepcopy(case.run)
    context_run.budget.model_calls = context_run.usage.model_calls + 4
    context_run.budget.model_tokens = (context_run.usage.model_tokens_used
                                       + context_run.usage.model_tokens_unknown + 200000)
    context_run.budget.decision_rounds = context_run.usage.decision_rounds + 4
    prepared = build_context(case.request, context_run, case.plan, results=results,
        delivery_snapshot=value, feedback={"new_result_ids": [case.result.id],
            "pending_step_ids": [], "delivery_snapshot": value},
        relevant_tools=case.run.permission.allowed_tools,
        now=case.run.created_at + timedelta(minutes=6), model_profile="thinking_low")
    data = json.dumps(payload(prepared)["DATA"], ensure_ascii=False)
    assert ANALYSIS_KIND in data and REJECTION_DETAIL in data
    assert prepared.input_token_bound <= case.run.budget.input_tokens == 64000
    record_property("input_token_bound", prepared.input_token_bound)
    assert (case.request, case.run, case.plan) == before


def test_bound_rejection_does_not_continue_with_unverified_source(bound_rejection_case, monkeypatch):
    case = bound_rejection_case
    original = delivery._source_integrity

    def unverified(store, result, **kwargs):
        integrity = original(store, result, **kwargs)
        if result.id == REJECTED_ANALYSIS_ID:
            integrity = {**integrity, "status": "unverified", "gaps": ["source_unverified:test"]}
        return integrity

    monkeypatch.setattr(delivery, "_source_integrity", unverified)
    selections, value = snapshot(case)
    assert all(selections[identifier]["source_integrity"]["status"] == "unverified"
               for identifier in case.goal_ids)
    assert not analysis_work(value)


def test_bound_rejection_requires_the_target_port_diagnostic(bound_rejection_case, monkeypatch):
    case = bound_rejection_case
    target = case.goal_ids[0]
    port = case.plan.goal_map[target].port
    original = case.store.load_result

    def without_target_rejection(run_id, result_id):
        result = original(run_id, result_id)
        if result_id == REJECTED_ANALYSIS_ID:
            result = result.model_copy(deep=True)
            result.diagnostics = [entry for entry in result.diagnostics
                if not (entry.get("category") == "observation_manifest_rejected" and entry.get("name") == port)]
        return result

    monkeypatch.setattr(case.store, "load_result", without_target_rejection)
    selections, value = snapshot(case)
    assert not selections[target]["analysis_source_candidates"]
    assert all(selections[identifier]["analysis_source_candidates"] for identifier in case.goal_ids[1:])
    target_ref = next(item["ref"] for item in value["goals"] if item["goal_id"] == target)
    assert not any(item["kind"] == "current_plan_work" and item["goal_ref"] == target_ref
                   for item in value["facts"])
    assert len(analysis_work(value)) == 2


def test_bound_rejection_cannot_borrow_valid_native_source_with_wrong_code_hash(bound_rejection_case, monkeypatch):
    case = bound_rejection_case
    original = case.store.load_result

    def wrong_code(run_id, result_id):
        result = original(run_id, result_id)
        if result_id == REJECTED_ANALYSIS_ID:
            result = result.model_copy(deep=True)
            result.source["code_sha256"] = "0" * 64
        return result

    monkeypatch.setattr(case.store, "load_result", wrong_code)
    selections, value = snapshot(case)
    assert all(not selections[identifier]["analysis_source_candidates"] for identifier in case.goal_ids)
    assert not analysis_work(value)
