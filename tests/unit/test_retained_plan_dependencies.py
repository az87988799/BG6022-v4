"""Frozen Plan dependency facts stay visible; no model or generated execution."""

import copy
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from orca_agent import context
from orca_agent.delivery import collect_delivery_snapshot
from orca_agent.models import Plan, Request, Run
from orca_agent.proposals import ProposedPlan, materialize_plan
from tests.unit.test_generated_delivery import case, written


@pytest.fixture
def live018():
    path = Path(__file__).parents[1] / "fixtures/code_contracts/live018-missing-retained-dependency.json"
    return json.loads(path.read_text(encoding="utf-8"))


def test_actual_retained_steps_show_dependency_closure_without_changing_plan(live018):
    plan = Plan.model_validate(live018["plan"])
    before = plan.model_dump(mode="json")
    frozen = {step.id: step.model_dump(mode="json") for step in plan.steps}
    code = context._plan(plan, frozen, retain_dependencies=True)
    assert code["steps"] == [{"id": step.id, "immutable_frozen": True, "depends_on": step.depends_on}
                             for step in plan.steps]
    assert code["steps"][0]["depends_on"] == []
    assert code["steps"][1]["depends_on"] == [plan.steps[0].id]
    # The old non-code projection stays byte-for-byte structurally unchanged.
    legacy = context._plan(plan, frozen)
    assert legacy["steps"] == [{"id": step.id, "immutable_frozen": True} for step in plan.steps]
    assert code["immutable_frozen_details_sha256"] == legacy["immutable_frozen_details_sha256"]
    assert plan.model_dump(mode="json") == before


@pytest.mark.parametrize("schema_columns", [False, True])
def test_compact_plan_ids_dependencies_and_empty_lists_are_literal(live018, schema_columns):
    plan = Plan.model_validate(live018["plan"])
    frozen = {step.id: step.model_dump(mode="json") for step in plan.steps}
    projection = context._plan(plan, frozen, retain_dependencies=True)
    template = {"AUTHORITY": {"plan": projection, "permission": {"code_execution": True}},
        "ACTION_SCHEMAS": {"revise_plan": ProposedPlan.model_json_schema()}, "TOOLS": [],
        "DATA": {"results": [{"plan": {"steps": [
            {"id": step.id, "depends_on": step.depends_on} for step in plan.steps]},
            "untrusted": True} for _ in range(20)]}}
    before = copy.deepcopy(template)
    wire = context._compact_native_wire(template, schema_columns=schema_columns)
    # Check raw wire before any alias/pool decoder; DATA cannot hide these paths.
    assert isinstance(wire["AUTHORITY"]["plan"]["steps"], list)
    for item, original in zip(wire["AUTHORITY"]["plan"]["steps"], plan.steps, strict=True):
        assert item["id"] == original.id
        assert item["depends_on"] == original.depends_on
    assert wire["AUTHORITY"]["permission"] == template["AUTHORITY"]["permission"]
    assert template == before


def test_actual_candidate_remains_invalid_without_automatic_dependency_insertion(live018):
    request = Request.model_validate(live018["request"])
    plan = Plan.model_validate(live018["plan"])
    run = Run.model_validate(live018["run_context"])
    proposal = copy.deepcopy(live018["proposal"])
    before = copy.deepcopy((request, plan, run, proposal))
    store = SimpleNamespace(load_request=lambda _: request, load_plan=lambda _: plan)
    # Its JSON shape is valid; omitted graph dependencies cause rejection later.
    ProposedPlan.model_validate(proposal["parameters"])
    with pytest.raises(ValueError, match="dependenc"):
        materialize_plan(store, run, proposal["parameters"])
    assert (request, plan, run, proposal) == before


@pytest.mark.parametrize("input_tokens", [12000, 64000])
def test_compact_writer_context_preserves_contract_or_fails_closed(tmp_path, monkeypatch, input_tokens):
    store, original, request, writer = case(tmp_path, monkeypatch)
    plan = store.load_plan(original)
    run = store.create_run(request, plan, original.permission.model_copy(update={"model_execution": True}),
        original.budget.model_copy(update={"model_calls": 4, "model_tokens": 100000,
            "input_tokens": input_tokens, "output_tokens": 2000, "decision_rounds": 12}),
        configuration=original.configuration.model_copy(update={"model":
            original.configuration.model.model_copy(update={"input_tokens": input_tokens})}))
    result, _ = written(store, run, writer)
    run.processed_feedback.append(result.id)
    snapshot = collect_delivery_snapshot(store, run, request, plan, [result])
    before = copy.deepcopy((request, run, plan, result, snapshot))
    persisted_before = store.path(f"runs/{run.id}/run.json").read_bytes()
    prepare = context.prepare_request
    calls = []

    def compact_only(messages, **kwargs):
        calls.append(json.loads(messages[1]["content"]))
        if len(calls) == 1:
            raise ValueError("conservative input token bound exceeds forced-first-view-test")
        return prepare(messages, **kwargs)

    monkeypatch.setattr(context, "prepare_request", compact_only)
    if input_tokens == 12000:
        # v53's complete code/observation guidance exceeds this old envelope.
        # Each parameter creates a separate Run; the failed Run is not enlarged.
        with pytest.raises(context.ContextLimitError, match="required model context exceeds"):
            context.build_context(request, run, plan, results=[result], delivery_snapshot=snapshot)
    else:
        prepared = context.build_context(request, run, plan, results=[result], delivery_snapshot=snapshot)
        assert prepared.input_token_bound <= input_tokens
        assert "Retain depends_on transitively, including completed Steps." in prepared.body()["messages"][0]["content"]
    assert len(calls) > 1
    for wire in calls:
        assert "ACTION_SCHEMAS" in wire and "PROPOSAL_SCHEMA" not in wire
        assert wire["AUTHORITY"]["plan"]["steps"][0]["id"] == writer.id
        assert wire["AUTHORITY"]["plan"]["steps"][0]["depends_on"] == []
        assert "string_steps" not in wire["AUTHORITY"]["plan"]
    assert (request, run, plan, result, snapshot) == before
    assert run.budget.input_tokens == run.configuration.model.input_tokens == input_tokens
    assert store.path(f"runs/{run.id}/run.json").read_bytes() == persisted_before
    assert not run.model_records and run.usage.model_calls == 0
