"""Effects in explanations remain bound to the actual settled invocation."""

import json
from datetime import timedelta
from types import SimpleNamespace

import pytest

from orca_agent.context import build_context
from orca_agent.models import Result, Step, ToolCall
from orca_agent.tools.registry import get_tool
from tests.unit.test_context import objects, payload


def settled(*, planned):
    request, run = objects(scientific=False)
    name = "evidence.import" if planned else "evidence.search"
    parameters = ({"source_id": "registered_source"} if planned else {
        "artifact_id": "geometry1", "query": "ENERGY", "start_line": 1,
        "max_lines": 200, "max_hits": 50, "case_sensitive": False})
    step = Step(id="import_step", logical_id="import_logical", tool=name, parameters=parameters) if planned else None
    call = ToolCall(tool=name, parameters=parameters, step_id=step.id if step else None,
                    frozen_step=step, state="completed", request_version=request.version)
    result = Result(run_id=run.id, call_id=call.id, step_id=call.step_id,
                    operation_status="completed", observations={"raw": {"units": None}})
    call.result_id = result.id
    run.calls = [call]
    run.result_ids = [result.id]
    request.conditions["explain_results"] = True
    run.goal_status = {request.goals[0].id: "satisfied"}
    return request, run, result


@pytest.mark.parametrize("planned", [False, True])
def test_settled_effects_visible_even_without_catalog_and_without_plan(planned):
    request, run, result = settled(planned=planned)
    before = request.model_dump_json(), run.model_dump_json(), result.model_dump_json()
    prepared = build_context(request, run, results=[result])
    data = payload(prepared)
    assert data["TOOL_CATALOG"] == []
    visible = data["DATA"]["results"][0]
    assert visible["tool"] == run.calls[0].tool
    effects = data["DATA"]["tool_effects"][visible["tool"]]
    assert effects == get_tool(run.calls[0].tool).effects
    assert effects == (["import_artifact"] if planned else ["read_registered_artifact"])
    assert visible["unqualified_observations"]["raw"]["units"] is None
    assert "qualified_outputs" not in visible
    assert (request.model_dump_json(), run.model_dump_json(), result.model_dump_json()) == before


@pytest.mark.parametrize("mismatch", ["run", "call", "result", "step", "state", "tool", "parameters"])
def test_execution_effects_never_borrow_an_unbound_call(mismatch):
    request, run, result = settled(planned=True)
    call = run.calls[0]
    if mismatch == "run":
        result.run_id = "another_run"
    elif mismatch == "call":
        result.call_id = "another_call"
    elif mismatch == "result":
        call.result_id = "another_result"
    elif mismatch == "step":
        call.step_id = "another_step"
    elif mismatch == "state":
        call.state = "unknown"
    elif mismatch == "tool":
        call.tool = "evidence.search"
    else:
        call.parameters["source_id"] = "another_source"
    visible = payload(build_context(request, run, results=[result]))["DATA"]["results"][0]
    assert "effects" not in visible
    assert "tool" not in visible


@pytest.mark.parametrize("run_id", ["run_0c4467ae0dce499694f154c8b08dbc50", "run_d00837758b9e4da48933f6a4d57a2402"])
def test_retained_query_import_results_keep_their_own_tool_and_effects(run_id):
    from orca_agent.store import sha256_file
    from tests.unit.test_context_actual import archived

    store, run, request, plan, results = archived(run_id)
    paths = list(store.path(f"runs/{run.id}").rglob("*.json"))
    hashes = {path: sha256_file(path) for path in paths}
    body = json.loads(store.path(f"runs/{run.id}/model/{run.model_records[-1]['id']}.request.json").read_text(encoding="utf-8"))
    original_usage = payload(SimpleNamespace(body=lambda: body))["AUTHORITY"]["cumulative_usage"]
    # Reconstruct only the in-memory pre-final-request view; no new call or
    # persisted accounting change is performed by this read-only replay.
    run = run.model_copy(deep=True)
    for name in ("model_calls", "model_tokens_used", "model_tokens_unknown"):
        setattr(run.usage, name, original_usage.get(name, 0))
    prepared = build_context(request, run, plan, results=results,
                             now=run.created_at + timedelta(seconds=30))
    data = payload(prepared)
    assert prepared.input_token_bound <= 12000
    for original, visible in zip(results, data["DATA"]["results"], strict=True):
        call = next(call for call in run.calls if call.id == original.call_id)
        assert call.result_id == visible["result_id"]
        assert visible["tool"] == call.tool
        assert data["DATA"]["tool_effects"][visible["tool"]] == get_tool(call.tool).effects
    assert {path: sha256_file(path) for path in paths} == hashes
