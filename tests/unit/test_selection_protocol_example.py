"""The shown envelope is complete strict JSON, never a transport repair."""

import json

import pytest

from orca_agent import context
from orca_agent.condition_selection import OptionalSelectionParameters, selectable_conditions
from orca_agent.delivery import collect_delivery_snapshot
from orca_agent.proposals import DecisionIntent
from orca_agent.store import Store
from tests.unit.test_code_context import selection_context
from tests.unit.test_decision_purpose_capacity import decoded


@pytest.mark.parametrize("method_selected", [False, True])
def test_pending_selection_shows_complete_three_field_current_intent(tmp_path, method_selected):
    request, run, _, _ = selection_context(tmp_path, general=True, simplified=True)
    request.version = run.request_version = 4
    if method_selected:
        request.method = "explicit_method"
        request.conditions_source["method"] = "explicit"
    snapshot = collect_delivery_snapshot(Store(tmp_path / "data"), run, request)
    prepared = context.build_context(request, run, delivery_snapshot=snapshot)
    wire = decoded(prepared)
    rules = wire["CONDITION_SELECTION_RULES"]
    example = json.loads(rules.split("Complete DecisionIntent JSON example: ", 1)[1])
    intent = DecisionIntent.model_validate(example)
    assert set(example) == {"action", "parameters", "reason"}
    assert intent.action == "select_conditions"
    assert intent.reason == "<overall_decision_reason>"
    assert set(intent.parameters) == {"request_version", "choices"}
    assert "reason" not in intent.parameters
    parameters = OptionalSelectionParameters.model_validate(intent.parameters)
    assert parameters.request_version == request.version == 4
    assert list(parameters.choices) == ["basis" if method_selected else "method"]
    assert set(parameters.choices) <= set(selectable_conditions(request))
    assert next(iter(parameters.choices.values())).value == "<chosen_value>"
    assert prepared.prompt_version == "agent-json-v83"


@pytest.mark.parametrize("simplified,authorized", [(False, True), (True, False)])
def test_legacy_or_unavailable_selection_has_no_new_example(tmp_path, simplified, authorized):
    _, _, _, wire = selection_context(tmp_path, general=True, simplified=simplified, authorized=authorized)
    assert "Complete DecisionIntent JSON example:" not in wire.get("CONDITION_SELECTION_RULES", "")
