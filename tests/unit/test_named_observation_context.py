"""Collector facts survive the full model-request projection, without execution."""

import copy
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from orca_agent import context, readable_context
from orca_agent.delivery import collect_delivery_snapshot
from orca_agent.models import (
    BudgetLimits,
    ModelLimits,
    PermissionSnapshot,
    Request,
    Run,
    RunConfiguration,
)
from orca_agent.tools.registry import get_tool
from tests.unit.test_context import payload


@pytest.mark.parametrize("force_fallback,deferred", [(False, False), (True, False), (False, True)])
def test_final_sent_view_preserves_only_registry_collector_explanation(monkeypatch, force_fallback, deferred):
    fixture = Path(__file__).parents[1] / "fixtures/code_contracts/live026-native-goal-output-route.json"
    request = Request.model_validate(json.loads(fixture.read_bytes())["request"])
    names = ["workspace.write", "python.execute", "orca.run", "evidence.text"]
    run = Run(request_id=request.id, request_version=request.version,
        permission=PermissionSnapshot(model_execution=True, code_execution=True,
            scientific_execution=True, artifact_writes=True, allowed_tools=names),
        budget=BudgetLimits(model_calls=48, model_tokens=1200000, input_tokens=64000,
            output_tokens=2000, file_writes=32, analysis_executions=12,
            decision_rounds=48, plan_revisions=12, evidence_reads=24),
        configuration=RunConfiguration(workflow_policy="simplified-1",
            observation_delivery_policy="reported-observation-2",
            model=ModelLimits(input_tokens=64000, output_tokens=2000)))
    snapshot = collect_delivery_snapshot(None, run, request, results=[])
    before = copy.deepcopy((request, run, snapshot))
    prepare = context._prepare_context_request
    attempted = []
    readable = readable_context.readable_context

    def defer_schema(template, *args, **kwargs):
        template = copy.deepcopy(template)
        for tool in template["TOOL_CATALOG"]:
            if tool["name"] == "orca.run":
                tool["parameter_schema"] = "deferred; existing validated parameters only"
        return readable(template, *args, **kwargs)

    if deferred:
        monkeypatch.setattr(readable_context, "readable_context", defer_schema)

    def capture(messages, **kwargs):
        body = payload(SimpleNamespace(body=lambda: {"messages": messages}))
        if "TOOLS" in body:
            attempted.append(body)
            if force_fallback and len(attempted) == 1:
                raise ValueError("conservative input token bound exceeds fixture first-view capacity")
        return prepare(messages, **kwargs)

    monkeypatch.setattr(context, "_prepare_context_request", capture)
    prepared = context.build_context(request, run, relevant_tools=names, delivery_snapshot=snapshot)
    final = payload(prepared)
    assert prepared.input_token_bound <= run.budget.input_tokens
    assert len(attempted) >= (2 if force_fallback else 1)
    for view in [*attempted, final]:
        projected = {tool["name"]: tool for tool in view["TOOLS"]}
        for name, capability in [("python.execute", "task_manifest"), ("orca.run", "none")]:
            tool = projected[name]
            definition = get_tool(name)
            assert tool.get("named_observation_collector") == capability, tool
            assert tool["output_contract"] == definition.parameter_schema["properties"]["observation_names"]["description"]
            assert "check_contract" not in tool
            if deferred and name == "orca.run":
                assert tool["parameters"] == "deferred; existing validated parameters only"
            else:
                assert "description" not in tool["parameters"]["properties"]["observation_names"]
        ordinary = projected["workspace.write"]
        assert "named_observation_collector" not in ordinary and "output_contract" not in ordinary
    assert (request, run, snapshot) == before

