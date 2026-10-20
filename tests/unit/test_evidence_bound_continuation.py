"""Current work prevents premature stop; archived probes never execute science."""

from copy import deepcopy
from datetime import timedelta
from pathlib import Path

import pytest

from orca_agent import model_usage
from orca_agent.context import build_context
from orca_agent.delivery import collect_goal_selections, delivery_snapshot
from orca_agent.proposals import ProposalError
from orca_agent.store import Store, StoreError
from orca_agent.terminal import (
    _authenticated_terminal_cost,
    validate_continuation_before_stop,
    validate_terminal_explanation,
)
from tests.unit.test_context import payload
from tests.unit.test_pending_plan_continuation import pending_report
from tests.unit.test_terminal_contract import valid_parameters


def pending_snapshot():
    request, run, plan, _ = pending_report()
    request.conditions["explain_results"] = True
    return request, run, delivery_snapshot(request, run, {}, plan=plan)


def test_scoped_valid_stop_cannot_replace_approved_work():
    request, run, snapshot = pending_snapshot()
    validate_terminal_explanation(valid_parameters(snapshot), snapshot)
    with pytest.raises(ProposalError) as caught:
        validate_continuation_before_stop(request, run, snapshot)
    assert caught.value.detail["code"] == "terminal_current_work_remaining"
    assert caught.value.detail["goal_refs"] == ["g1"]


@pytest.mark.parametrize("remaining", ["calls", "tokens", "rounds", "time"])
def test_real_final_delivery_margin_allows_honest_partial_without_changing_snapshot(remaining, monkeypatch):
    request, run, snapshot = pending_snapshot()
    if remaining == "calls":
        run.usage.model_calls = run.budget.model_calls - 1
    elif remaining == "tokens":
        run.usage.model_tokens_used = run.budget.model_tokens - 1
    elif remaining == "rounds":
        run.usage.decision_rounds = run.budget.decision_rounds
    else:
        monkeypatch.setattr(model_usage, "utc_now", lambda: run.deadline - timedelta(seconds=1))
    before = deepcopy(snapshot)
    validate_continuation_before_stop(request, run, snapshot)
    assert snapshot == before


@pytest.fixture(scope="module")
def archived_failure():
    root = Path(__file__).parents[2] / "data/live-repair/attempts/a009/data"
    identifier = "run_818c9ee14bd84459a0197fd63e08637a"
    if not (root / "runs" / identifier / "run.json").is_file():
        pytest.skip("local immutable a009 archive is not available")
    store = Store(root)
    run = store.load_run(identifier)
    request, plan = store.load_request(run), store.load_plan(run)
    selections = collect_goal_selections(store, run, request, plan)
    return store, request, run, plan, selections


def test_actual_a009_failed_inner_has_one_shared_diagnostic_and_requires_followup(archived_failure, monkeypatch):
    store, request, run, plan, selections = archived_failure
    before = run.model_dump_json()
    snapshot = delivery_snapshot(request, run, selections, plan=plan)
    work = [item for item in snapshot["facts"] if item["kind"] == "current_plan_work"]
    assert {item["goal_ref"] for item in work} == {"g3", "g4", "g5", "g6"}
    references = {item["value"]["work_ref"] for item in work}
    assert len(references) == 1
    evidence = snapshot["references"][references.pop()]
    assert evidence["route"] == "new_followup_step"
    assert evidence["outer_operation_status"] == "completed"
    assert evidence["new_orca_starts_required"] == 1
    assert evidence["internal_calls"][0]["request_key"] == "nmr_shielding"
    stderr = next(row for row in evidence["diagnostics"] if row["filename"] == "stderr.txt")
    assert stderr["sha256"] == "335ef014c65d417bdb27e4734e64b3874adf61f5ff1434bee3adcd2d6bacd8f1"
    assert "Unknown identifier in EPRNMR" in stderr["text"] and "NMR" in stderr["text"]
    monkeypatch.setattr(model_usage, "utc_now", lambda: run.created_at + timedelta(minutes=6))
    with pytest.raises(ProposalError, match="current goal/fact") as caught:
        validate_continuation_before_stop(request, run, snapshot)
    assert caught.value.detail["code"] == "terminal_current_work_remaining"
    parameters = deepcopy(valid_parameters(snapshot))
    parameters["delivery"]["goal_explanations"][2]["fact_refs"].remove(work[0]["ref"])
    with pytest.raises(ProposalError) as caught:
        validate_terminal_explanation(parameters, snapshot)
    assert caught.value.detail["code"] == "terminal_fact_scope"
    results = [store.load_result(run.id, identifier) for identifier in run.selected_results.values()]
    prepared = build_context(request, run, plan, results=results, delivery_snapshot=snapshot,
        feedback={"new_result_ids": [selections[request.goals[2].id]["result"].id],
                  "pending_step_ids": [], "delivery_snapshot": snapshot},
        relevant_tools=run.permission.allowed_tools,
        now=run.created_at + timedelta(minutes=6), model_profile="thinking_low")
    wire = payload(prepared)
    assert prepared.input_token_bound <= run.budget.input_tokens == 64000
    assert "failure_retry" in str(wire["DATA"]["delivery"])
    assert stderr["sha256"] in str(wire["DATA"])
    assert run.model_dump_json() == before


@pytest.mark.parametrize("barrier", ["orca", "extra", "analysis", "write", "revision", "permission", "unknown", "optional", "bad_hash"])
def test_actual_failure_does_not_force_work_without_a_valid_current_route(archived_failure, barrier):
    _, request, run, plan, selections = deepcopy(archived_failure)
    if barrier in {"orca", "extra", "analysis", "write", "revision"}:
        name, usage = {"orca": ("orca_starts", "orca_starts_reserved"),
            "extra": ("extra_orca_starts", "extra_orca_starts_reserved"),
            "analysis": ("analysis_executions", "analysis_executions"),
            "write": ("file_writes", "file_writes"), "revision": ("plan_revisions", "plan_revisions")}[barrier]
        setattr(run.budget, name, getattr(run.usage, usage))
    elif barrier == "permission":
        run.permission.scientific_execution = False
    elif barrier == "unknown":
        run.internal_calls[0].state = "unknown"
    elif barrier == "optional":
        for goal in request.goals[2:]:
            goal.required = False
    else:
        for selection in selections.values():
            if "source_integrity" in selection:
                selection["source_integrity"]["status"] = "unverified"
    snapshot = delivery_snapshot(request, run, selections, plan=plan)
    assert not [item for item in snapshot["facts"] if item["kind"] == "current_plan_work"]


def test_completed_raw_evidence_repairs_manifest_without_scientific_execution(archived_failure):
    # A synthetic change of the local projection tests policy; it is never
    # written back or presented as a successful version of the real a009 run.
    _, request, run, plan, selections = deepcopy(archived_failure)
    run.permission.scientific_execution = run.permission.allow_additional_science = False
    run.budget.orca_starts = run.budget.extra_orca_starts = 0
    run.budget.code_retries = 0
    for selection in selections.values():
        evidence = selection.get("continuation_evidence")
        if evidence:
            for call in evidence["internal_calls"]:
                call["state"] = "completed"
            selection["assessment"]["reasons"] = ["requested_observation_missing"]
    snapshot = delivery_snapshot(request, run, selections, plan=plan)
    work = [item for item in snapshot["facts"] if item["kind"] == "current_plan_work"]
    assert len(work) == 4
    for item in work:
        evidence = snapshot["references"][item["value"]["work_ref"]]
        assert evidence["kind"] == "repair_existing_evidence"
        assert evidence["new_orca_starts_required"] == 0
    assert all("without starting ORCA" in action["text"] for action in snapshot["next_actions"][2:])


def test_outer_sdk_failure_before_native_submission_still_requires_science_budget(archived_failure):
    _, request, run, plan, selections = deepcopy(archived_failure)
    run.internal_calls = []
    run.usage.orca_starts_reserved = run.usage.orca_starts_actual = 0
    for selection in selections.values():
        evidence = selection.get("continuation_evidence")
        if evidence:
            evidence["state"] = "failed"
            evidence["internal_calls"] = []
    run.budget.orca_starts = 0
    snapshot = delivery_snapshot(request, run, selections, plan=plan)
    assert not [item for item in snapshot["facts"] if item["kind"] == "current_plan_work"]


@pytest.mark.parametrize("charge_reserved", [False, True])
def test_program_selected_terminal_request_with_real_cost_can_finish_after_settlement(archived_failure, tmp_path, monkeypatch, charge_reserved):
    archived, request, run, plan, selections = deepcopy(archived_failure)
    run.budget.model_tokens = run.usage.model_tokens_used + run.usage.model_tokens_unknown + 100000
    now = run.created_at + timedelta(minutes=6)
    monkeypatch.setattr(model_usage, "utc_now", lambda: now)
    snapshot = delivery_snapshot(request, run, selections, plan=plan)
    results = [archived.load_result(run.id, identifier) for identifier in run.selected_results.values()]
    prepared = build_context(request, run, plan, results=results, delivery_snapshot=snapshot,
        feedback={"new_result_ids": [selections[request.goals[2].id]["result"].id],
                  "pending_step_ids": [], "delivery_snapshot": snapshot},
        relevant_tools=run.permission.allowed_tools, now=now, model_profile="thinking_low")
    assert payload(prepared)["AUTHORITY"]["decision_purpose"]["kind"] == "terminal"
    store = Store(tmp_path / "data", environment_root=tmp_path / "environment")
    path = store.path(f"runs/{run.id}/model/offline_final.request.json")
    path.parent.mkdir(parents=True)
    path.write_text(prepared.canonical_body, encoding="utf-8", newline="")
    record = {"id": "offline_final", "request_hash": prepared.request_hash,
        "transport_limits": prepared.limits.model_dump(), "model_profile": prepared.model_profile,
        "input_reserved": prepared.input_token_bound, "output_reserved": prepared.output_token_bound}
    run.usage.model_tokens_used += prepared.reserved_tokens if charge_reserved else 1000
    # Omitting the cost of another real request incorrectly claims room remains.
    with pytest.raises(ProposalError):
        validate_continuation_before_stop(request, run, snapshot)
    cost = _authenticated_terminal_cost(store, run, record)
    assert cost is not None
    if not charge_reserved:
        from orca_agent.decision_purpose import DecisionPurpose

        assert model_usage.final_explanation_budget(request, run,
            purpose=DecisionPurpose("planning", ()), prepared=cost)["can_send_before_final"]
    validate_continuation_before_stop(request, run, snapshot, terminal_cost=cost)
    path.write_text(prepared.canonical_body + " ", encoding="utf-8", newline="")
    with pytest.raises(StoreError, match="request changed"):
        _authenticated_terminal_cost(store, run, record)
