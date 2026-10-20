"""Read-only regression of the real refreeze-3 failures; no model or ORCA calls."""

import gzip
import hashlib
import json
from copy import deepcopy
from datetime import timedelta
from pathlib import Path

import pytest

from orca_agent import context
from orca_agent.agent import _ready
from orca_agent.decision_purpose import relevant_result_ids
from orca_agent.delivery import collect_delivery_snapshot, collect_goal_facts
from orca_agent.models import OutputBinding, Run
from orca_agent.store import Store
from tests.unit.test_context import payload


def archived_context(task, *, repair_routes=False):
    root = Path(__file__).parents[2] / (
        f"docs/acceptance/code-as-tools-step7-refreeze-3/evidence/{task}/data")
    paths = list((root / "runs").glob("*/run.json"))
    store = Store(root)
    if task == "T3":
        fixture = Path(__file__).parents[1] / "fixtures/code_contracts/refreeze3-t3-run.json.gz"
        raw = gzip.decompress(fixture.read_bytes())
        provenance = json.loads(fixture.with_name("refreeze3-t3-run.provenance.json").read_bytes())
        assert hashlib.sha256(raw).hexdigest() == provenance["original_sha256"]
        run = Run.model_validate_json(raw)
        # The public archive indexes but omits the >512 KiB original Run. Use
        # its exact compressed bytes in memory; never write into the archive.
        load_run = store.load_run
        store.load_run = lambda identifier: run if identifier == run.id else load_run(identifier)
    else:
        assert len(paths) == 1, "committed real evidence must remain available for this regression"
        run = store.load_run(paths[0].parent.name)
    request, plan = store.load_request(run), store.load_plan(run)
    results = {row.step_id: row for row in (
        store.load_result(run.id, identifier) for identifier in run.selected_results.values())}
    feedback = [identifier for identifier in run.result_ids if identifier not in run.processed_feedback]
    if repair_routes:
        latest = store.load_result(run.id, feedback[-1])
        names = {record.name for record in latest.observation_records}
        for goal in request.goals:
            if goal.port in names:
                plan.goal_map[goal.id] = OutputBinding(step_id=latest.step_id, port=goal.port)
    snapshot = collect_delivery_snapshot(store, run, request, plan, results,
                                        control_generation=run.control_generation)
    selected = [store.load_result(run.id, identifier) for identifier in relevant_result_ids(
        run, plan, snapshot=snapshot, feedback_ids=feedback)]
    data = {"new_result_ids": feedback, "validation_error": None,
            "allowed_repairs": run.permission.allowed_repairs,
            "pending_step_ids": [step.id for step in _ready(plan, results)],
            "goal_facts": collect_goal_facts(store, run, request, plan, results),
            "delivery_snapshot": snapshot}
    from orca_agent.goals import current_goal_evidence
    current_use = []
    for goal in request.goals:
        selection = current_goal_evidence(store, run, request, goal, plan, results)
        if selection["result"] is not None:
            assessment = dict(selection["assessment"])
            if selection["gaps"]:
                assessment["status"] = "unresolved"
                assessment["reasons"] = assessment["reasons"] + selection["gaps"]
            current_use.append({"goal_id": goal.id, "result_id": selection["result"].id, **assessment})
    if current_use:
        data["current_goal_use"] = current_use
    return request, run, plan, {
        "results": selected, "feedback": data, "relevant_tools": run.permission.allowed_tools,
        "user_messages": store.read_control(run.id)["messages"], "delivery_snapshot": snapshot,
        "now": run.created_at + timedelta(seconds=30), "model_profile": "thinking_low",
    }


@pytest.mark.parametrize("task", ["T3", "T4", "H1"])
def test_real_result_feedback_fits_complete_frozen_request(task):
    request, run, plan, kwargs = archived_context(task)
    before = request.model_dump_json(), run.model_dump_json(), plan.model_dump_json()
    prepared = context.build_context(request, run, plan, **kwargs)
    wire = payload(prepared)
    assert prepared.input_token_bound <= run.budget.input_tokens == 64000
    assert prepared.input_token_bound <= prepared.limits.input_tokens
    assert wire["AUTHORITY"]["related_results"] == kwargs["feedback"]["new_result_ids"]
    visible = {row["result_id"]: row for row in wire["DATA"]["results"]}
    assert set(kwargs["feedback"]["new_result_ids"]) <= visible.keys()
    assert {goal["id"] for goal in wire["AUTHORITY"]["request"]["goals"]} == {
        goal.id for goal in request.goals}
    assert before == (request.model_dump_json(), run.model_dump_json(), plan.model_dump_json())


def test_repaired_t3_goal_routes_keep_actual_goal_feedback_within_frozen_capacity():
    request, run, plan, kwargs = archived_context("T3", repair_routes=True)
    assert len(kwargs["feedback"]["current_goal_use"]) == len(request.goals)
    prepared = context.build_context(request, run, plan, **kwargs)
    assert prepared.input_token_bound <= 64000
    wire = payload(prepared)
    # The delivery snapshot is the authoritative equivalent of current_goal_use;
    # the context deliberately avoids repeating these assessments a second time.
    assert {row["request_goal_id"] for row in wire["DATA"]["delivery"]["goals"]} == {
        row["goal_id"] for row in kwargs["feedback"]["current_goal_use"]}
    bindings = wire["AUTHORITY"]["current_input_bindings"]
    references = [row for row in wire["DATA"]["results"] if row.get("input_binding_refs")]
    assert len(references) == 2
    for row in references:
        for index in row["input_binding_refs"]:
            binding = bindings[index]
            original = next(result for result in kwargs["results"] if result.id == row["result_id"])
            output = original.qualified_outputs[binding["port"]]
            assert binding["result_id"] == original.id
            assert (binding["artifact_id"], binding["sha256"]) == (output.artifact_id, output.source["sha256"])
            assert all(check.status == "passed" and check.rule_version == binding["rule_version"]
                       for check in output.checks)
    # Current feedback keeps its body even if the same input was already seen.
    rows = [context._result(result, 256) for result in kwargs["results"]]
    context._compact_result_facts(rows)
    before = deepcopy(rows)
    assert not context._reference_current_input_facts(rows, kwargs["results"], run, bindings,
                                                     [result.id for result in kwargs["results"]])
    assert rows == before


def test_capacity_failure_reports_each_complete_request_without_prompt_contents():
    from tests.unit.test_context import objects

    request, run = objects()
    run.budget.input_tokens = 1
    with pytest.raises(context.ContextLimitError) as caught:
        context.build_context(request, run)
    diagnosis = caught.value.diagnostics
    assert diagnosis["candidate_count"] == len(diagnosis["candidates"]) > 0
    assert diagnosis["run_input_limit"] == 1
    for candidate in diagnosis["candidates"]:
        assert candidate["input_token_bound"] == candidate["content_bytes"] + candidate["framing_bound"]
        assert candidate["sections"]["AUTHORITY"] > 0
    assert request.original_text not in json.dumps(diagnosis) if request.original_text else True


def test_many_ordinary_diagnostics_share_a_single_bounded_projection():
    diagnostics = [{"category": "observation_manifest_rejected",
                    "detail": "invalid observation " * 20} for _ in range(32)]
    before = deepcopy(diagnostics)
    view = context._diagnostic_records(diagnostics, 256)
    assert len(context._json(view).encode()) <= 256
    assert view[0]["category"] == "diagnostics_projection"
    assert view[0]["count"] == 32 and view[0]["bytes"] > 12000
    assert diagnostics == before


@pytest.mark.parametrize("mismatch", [
    None, "logical", "number", "time", "result_attempt", "result_status", "frozen_identity",
])
def test_historical_failure_compaction_requires_later_bound_same_logical_attempt(mismatch):
    _, run, _, kwargs = archived_context("T3")
    results = kwargs["results"]
    summary = context._superseded_failures(run, results)
    assert summary
    failed_id, entry = next(iter(summary.items()))
    failed = next(row for row in run.attempts if row.id == failed_id)
    replacement = next(row for row in run.attempts if row.id == entry["superseded_by"])
    result = next(row for row in results if row.id == replacement.result_id)
    if mismatch == "logical":
        replacement.logical_id = "unrelated_task"
    elif mismatch == "number":
        replacement.number = failed.number
    elif mismatch == "time":
        replacement.finished_at = failed.finished_at
    elif mismatch == "result_attempt":
        result.attempt_id = "other_attempt"
    elif mismatch == "result_status":
        result.operation_status = "failed"
    elif mismatch == "frozen_identity":
        replacement.frozen_step.id = "other_step"
    actual = context._superseded_failures(run, results)
    assert (failed_id in actual) is (mismatch is None)
    if mismatch is None:
        assert "science unqualified" in actual[failed_id]["meaning"]
        historical = next(row for row in results if row.id == failed.result_id)
        projected = context._result(historical, 256)
        assert projected["operation_status"] == historical.operation_status == "failed"
        assert projected["result_id"] == historical.id and projected["source"]
        for name, checks in historical.checks.items():
            for check in checks:
                if check.status != "passed":
                    assert projected["checks"][name]["nonpassing"][check.name] == check.status


def test_inventory_projection_continues_after_visible_rows_without_claiming_complete():
    artifacts = [{"id": f"log_{number}", "basename": "stdout.out", "sha256": "a" * 64,
                  "role": "raw_evidence", "size": 100, "integrity": "verified",
                  "internal_request_key": f"child_{number}", "operation_status": "failed"}
                 for number in range(40)]
    full = {"run_id": "original_run", "inventory_sha256": "b" * 64, "artifacts": artifacts,
            "offset": 0, "total": 40, "next_offset": None, "status": "complete",
            "coverage": {"complete": True, "returned": {"start": 0, "stop": 40}}}
    original = deepcopy(full)
    view = context._evidence_observation(full, 256)
    assert 0 < len(view["artifacts"]) < 40
    assert view["status"] == "partial" and not view["coverage"]["complete"]
    assert view["tool_coverage"] == full["coverage"]
    assert view["next_offset"] == len(view["artifacts"])
    assert view["coverage"]["returned"]["stop"] == view["next_offset"]
    assert view["continuation"]["parameters"]["offset"] == view["next_offset"]
    assert view["artifacts"][0]["internal_request_key"] == "child_0"
    assert view["artifacts"][0]["operation_status"] == "failed"
    # A following Tool page starts at the context cursor, not the original
    # complete response's end; all omitted rows remain discoverable.
    following = {**full, "offset": view["next_offset"], "artifacts": artifacts[view["next_offset"]:]}
    second = context._evidence_observation(following, 256)
    ids = [row["id"] for row in [*view["artifacts"], *second["artifacts"]]]
    assert ids == [row["id"] for row in artifacts[:len(ids)]]
    assert len(ids) == len(set(ids)) and full == original
