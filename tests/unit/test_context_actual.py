"""Read-only replays of retained development records; absent local archives skip.

These tests never send HTTP, run science, or rewrite the original failed Run.
They verify current context construction, not a successful rerun of that model.
"""

from datetime import timedelta
from pathlib import Path

import pytest

from orca_agent.context import build_context
from orca_agent.store import Store, sha256_file
from tests.unit.test_context import payload

REFERENCE = Path(__file__).resolve().parents[2] / "data/phase-b/reference"
ARRAY_RUN = "run_3f034e91428f48da823051b3acff6d43"
IMPORT_RUN = "run_4c8e701eedd2489eacb2c68d0b287c33"
SAMPLING_RUNS = ["run_ce0a52bb422a4238ae42a82f8010dd01", "run_0217d5b477c141a2be73cd097bddcd7e"]


def archived(run_id, root=REFERENCE):
    if not (root / "runs" / run_id / "run.json").is_file():
        pytest.skip("local real-development archive is absent; no replacement evidence is fabricated")
    store = Store(root)
    run = store.load_run(run_id)
    request, plan = store.load_request(run), store.load_plan(run)
    results = [store.load_result(run.id, result_id) for result_id in run.result_ids]
    return store, run, request, plan, results


def test_actual_array_slice_feedback_keeps_pending_scalar_and_fits_bound():
    store, run, request, plan, results = archived(ARRAY_RUN)
    before = run.model_dump_json(), sha256_file(store.path(f"runs/{run.id}/run.json"))
    pending = next(step for step in plan.steps if step.id not in run.selected_results)
    pending_parameters = pending.parameters.model_dump(mode="json")
    latest = next(result for result in results if result.id not in run.processed_feedback)
    assert latest.id == "result_b28345d440fe431aa97d7135b34a02de"
    assert pending_parameters["path"][-1] == {"kind": "key", "key": "dipoleMagnitude"}
    prepared = build_context(
        request, run, plan, results=results, relevant_tools=run.permission.allowed_tools,
        feedback={"new_result_ids": [latest.id], "pending_step_ids": [pending.id]},
        now=run.created_at + timedelta(seconds=30),
    )
    data = payload(prepared)
    assert prepared.input_token_bound <= 12000
    authority = data["AUTHORITY"]
    step = next(item for item in authority["plan"]["steps"] if item["id"] == pending.id)
    assert step["parameters"]["artifact_id"] == pending_parameters["artifact_id"]
    assert step["parameters"]["path"] == pending_parameters["path"]
    assert authority["related_results"] == [latest.id]
    assert data["CONTROL"]["pending_step_ids"] == [pending.id]
    visible = next(item for item in data["DATA"]["results"] if item["result_id"] == latest.id)
    observation = visible["unqualified_observations"]["value_observation"]
    original = latest.observations["value_observation"]
    assert observation["path"] == original["path"]
    assert observation["sha256"] == original["sha256"]
    assert observation["scientific_status"] == "unverified"
    assert observation["status"] == "observed"
    assert observation["value"] == original["value"][:len(observation["value"])]
    if observation["value"] != original["value"]:
        assert observation["value_projection"]["partial"] is True
        assert "empty preview does not mean empty source" in observation["value_projection"]["meaning"]
    assert (run.model_dump_json(), sha256_file(store.path(f"runs/{run.id}/run.json"))) == before


@pytest.mark.parametrize("run_id", SAMPLING_RUNS)
@pytest.mark.parametrize("phase", ["three_sp", "analysis_feedback"])
def test_actual_sampling_three_sp_and_analysis_preserve_facts_within_bound(run_id, phase):
    from orca_agent.agent import _ready

    store, run, request, plan, results = archived(run_id, REFERENCE.parent / "agent")
    original_hash = sha256_file(store.path(f"runs/{run.id}/run.json"))
    assert len(run.attempts) == 3 and len(results) == 4
    if phase == "three_sp":
        # Rewind only this in-memory view; no record or budget is rewritten.
        run.calls.pop()
        results.pop()
        run.result_ids = [result.id for result in results]
    before = run.model_dump_json()
    ready = [step.id for step in _ready(plan, {result.step_id: result for result in results})]
    prepared = build_context(request, run, plan, results=results,
        feedback={"new_result_ids": [results[-1].id], "pending_step_ids": ready},
        relevant_tools=run.permission.allowed_tools, user_messages=store.read_control(run.id)["messages"],
        now=run.created_at + timedelta(seconds=30))
    data = payload(prepared)
    assert prepared.input_token_bound <= 12000
    assert data["AUTHORITY"]["request"]["goals"][0]["conditions"] == request.goals[0].conditions
    assert data["AUTHORITY"]["permission"] == run.permission.model_dump(mode="json")
    assert data["AUTHORITY"]["related_results"] == [results[-1].id]
    for original, projected in zip(results, data["DATA"]["results"], strict=True):
        if "energy" in original.qualified_outputs:
            assert projected["qualified_outputs"]["energy"]["value"] == original.qualified_outputs["energy"].value
            assert projected["qualified_outputs"]["energy"]["unit"] == "Eh"
            assert len(projected["source_record_sha256"]) == 64
        else:
            observation = projected["unqualified_observations"]["analysis"]
            assert observation["reason"] == original.observations["analysis"]["reason"]
            assert len(observation["projection_sha256"]) == 64
            assert [member.get("energy_eh") for member in observation["members"]] == [
                member["energy_eh"] for member in original.observations["analysis"]["members"]]
    assert run.model_dump_json() == before
    assert sha256_file(store.path(f"runs/{run.id}/run.json")) == original_hash


def test_actual_import_stop_budget_labels_include_this_transmission_without_mutation():
    store, persisted, request, plan, results = archived(IMPORT_RUN)
    original_hash = sha256_file(store.path(f"runs/{persisted.id}/run.json"))
    assert persisted.usage.model_calls == 3 and persisted.budget.model_calls == 4
    # Reconstruct only the context before the archived third/stop transmission.
    # This is an in-memory replay, not a modification of durable usage.
    run = persisted.model_copy(deep=True)
    final = run.model_records.pop()
    run.usage.model_calls -= 1
    run.usage.model_tokens_used -= final["total_tokens"]
    before = run.model_dump_json()
    prepared = build_context(request, run, plan, results=results,
        feedback={"new_result_ids": [results[-1].id]},
        relevant_tools=run.permission.allowed_tools, now=run.created_at + timedelta(seconds=30))
    authority = payload(prepared)["AUTHORITY"]
    assert authority["remaining"]["model_calls_after_this_request"] == 1
    assert "model_calls" not in authority["remaining"]
    assert authority["cumulative_usage"]["as_of"] == "before_this_request"
    assert authority["cumulative_usage"]["model_calls"] == 2
    assert authority["remaining"]["model_tokens_before_this_request"] == (
        run.budget.model_tokens - run.usage.model_tokens_used - run.usage.model_tokens_unknown)
    assert "model_tokens" not in authority["remaining"]
    assert run.model_dump_json() == before
    assert sha256_file(store.path(f"runs/{persisted.id}/run.json")) == original_hash


@pytest.mark.parametrize("diagnostic", ["persisted", "structured_ready_ids"])
def test_actual_array_correction_preserves_native_control_within_bound(diagnostic):
    store, run, request, plan, results = archived("run_a86f88a16a6648639c9688061c59f1c9")
    before = run.model_dump_json(), sha256_file(store.path(f"runs/{run.id}/run.json"))
    pending = [step.id for step in plan.steps if step.id not in run.selected_results]
    assert len(pending) == 1
    latest = [result.id for result in results if result.id not in run.processed_feedback]
    rejected = next(item for item in reversed(run.decisions) if item.get("action") == "rejected")
    error = {"category": rejected["parameters"]["error_category"],
             "requirement": rejected["parameters"]["requirement"]}
    if diagnostic == "structured_ready_ids":
        # Replay the current program-authored diagnostic against the same real
        # failed context; do not relabel the original persisted response.
        error = {"category": "ProposalError", "requirement": {
            "requirement": "Choose an exact ID from expected_ready_step_ids. A Step with a settled Result "
                           "cannot be executed again; a Step whose dependencies are not ready cannot execute.",
            "path": ["parameters", "step_id"], "expected_ready_step_ids": pending}}
    prepared = build_context(request, run, plan, results=results,
        relevant_tools=run.permission.allowed_tools,
        feedback={"new_result_ids": latest, "pending_step_ids": pending, "validation_error": error},
        now=run.created_at + timedelta(seconds=30))
    assert prepared.input_token_bound <= 12000
    # Native wire assertion intentionally does not decode SHARED_STRINGS.
    import json
    wire = json.loads(prepared.body()["messages"][1]["content"])
    assert wire["CONTROL"] == {"pending_step_ids": pending, "validation_error": error}
    assert wire["ACTION_PARAMETERS"]["call_tool"] == {"step_id": pending[0]}
    assert payload(prepared)["AUTHORITY"]["related_results"] == latest
    assert (run.model_dump_json(), sha256_file(store.path(f"runs/{run.id}/run.json"))) == before
