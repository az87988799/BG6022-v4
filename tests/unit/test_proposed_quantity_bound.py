"""a025 display-label ingress replay; no model, Tool or ORCA execution."""

import copy
import json
from pathlib import Path

import pytest
from pydantic import TypeAdapter, ValidationError
from test_simplified_semantic_selection import intake, normal, rebind, submit

from orca_agent.models import ScientificLabel
from orca_agent.proposals import ProposalError
from orca_agent.semantic import (
    SEMANTIC_TEXT_LIMIT,
    SemanticCandidate,
    action_parameters,
    source_fragments,
)
from orca_agent.store import StoreError

FIXTURE = Path(__file__).parents[1] / "fixtures/a025-long-quantity-rejection.json"


def actual(tmp_path, *, policy="simplified-1"):
    record = json.loads(FIXTURE.read_bytes())
    store, _, run = intake(tmp_path, record["original_text"], policy=policy)
    message = store.read_control(run.id)["messages"][0]
    old = {"id": record["parameters"]["message_ids"][0], "text": record["original_text"]}
    refs = {a["id"]: b["id"] for a, b in zip(source_fragments([old]), source_fragments([message]), strict=True)}

    def references(value):
        if isinstance(value, dict):
            return {key: [refs[ref] for ref in child] if key == "source_refs" else references(child)
                    for key, child in value.items()}
        return [references(item) for item in value] if isinstance(value, list) else value

    parameters = rebind(references(record["parameters"]), message["id"])
    return store, run, parameters


def test_actual_a025_long_proposed_label_is_normalized_after_source_authentication(tmp_path):
    store, run, parameters = actual(tmp_path)
    original = copy.deepcopy(parameters)
    assert len(parameters["goals"][1]["quantity"]) == 299
    updated = submit(store, run, parameters)
    request = store.load_request(updated)
    assert request.normalization_status == "normalized" and len(request.goals) == 2
    for goal, proposed in zip(request.goals, original["goals"], strict=True):
        assert proposed["text_basis"] in goal.original_text
        assert goal.original_text == goal.text_evidence["text_basis"]
        assert goal.quantity in goal.original_text and len(goal.quantity) <= 256
        assert goal.required == proposed.get("required", True)
        assert set(proposed.get("minimum_evidence", [])) <= set(goal.minimum_evidence)
        assert goal.text_evidence["quantity_canonicalization"]["proposed_value"] == proposed["quantity"]
    assert parameters == original
    assert updated.decisions[-1]["semantics"]["proposed_candidate"]["goals"][1]["quantity"] == original["goals"][1]["quantity"]
    assert not updated.calls and not updated.attempts and updated.usage.model_calls == 0


def test_long_literal_retains_full_energy_source_and_shared_canonicalization_audit(tmp_path):
    text = "请为乙醇" + "保留原文来源" * 50 + "。报告电子能。"
    store, config, run = intake(tmp_path, text)
    parameters = normal(store, config, run, quantity=text)
    updated = submit(store, run, parameters)
    goal, = store.load_request(updated).goals
    assert len(goal.quantity) <= 160 and "电子能" not in goal.quantity
    assert goal.original_text == text and goal.text_evidence["quantity_canonicalization"]["proposed_value"] == text
    assert goal.required and "电子能" in goal.original_text
    with pytest.raises(ValidationError):
        TypeAdapter(ScientificLabel).validate_python(text)


@pytest.mark.parametrize("change", ["source", "foreign_message", "over_limit", "non_string", "control", "blank",
                                  "leading_space"])
def test_wider_proposal_field_does_not_relax_source_or_label_type(tmp_path, change):
    store, run, parameters = actual(tmp_path)
    goal = parameters["goals"][1]
    if change == "source":
        goal["text_basis"] = "伪造的原文来源"
    elif change == "foreign_message":
        goal["message_id"] = "foreign"
    else:
        goal["quantity"] = {"over_limit": "量" * (SEMANTIC_TEXT_LIMIT + 1), "non_string": 123,
                            "control": "非法\n名称", "blank": "", "leading_space": " 前导空格"}[change]
    before = store.load_request(run).model_dump_json()
    with pytest.raises((ProposalError, StoreError)):
        submit(store, run, parameters)
    assert store.load_request(run).model_dump_json() == before
    assert not run.calls and not run.attempts


def test_old_or_untrusted_validation_context_keeps_persisted_bound(tmp_path):
    store, run, parameters = actual(tmp_path, policy=None)
    for context in (None, {"source_quantity_labels": True}, {"workflow_policy": "simplified-1"}):
        with pytest.raises(ValidationError):
            SemanticCandidate.model_validate(parameters, context=context)
    with pytest.raises(ProposalError):
        submit(store, run, parameters)
    assert store.load_request(run).version == 1


def test_prior_semantic_version_cannot_use_the_new_proposal_bound(tmp_path):
    store, run, parameters = actual(tmp_path)
    parameters["schema_version"] = "request-semantics-2"
    with pytest.raises(ProposalError) as caught:
        submit(store, run, parameters)
    assert caught.value.detail["errors"] == [{"type": "string_too_long",
        "loc": ["parameters", "goals", 1, "quantity"], "constraints": {"max_length": 256}}]
    assert store.load_request(run).version == 1


def test_candidate_schema_and_validator_share_only_the_display_input_bound():
    def quantities(value):
        if isinstance(value, dict):
            if "quantity" in value.get("properties", {}):
                yield value["properties"]["quantity"]
            for child in value.values():
                yield from quantities(child)
        elif isinstance(value, list):
            for child in value:
                yield from quantities(child)

    old = action_parameters(code_authorized=True)["normalize_request"]["schema"]
    new = action_parameters(code_authorized=True, workflow_simplified=True)["normalize_request"]["schema"]
    old_quantity, = quantities(old)
    new_quantity, = quantities(new)
    old_string = next(item for item in old_quantity["anyOf"] if item["type"] == "string")
    new_string = next(item for item in new_quantity["anyOf"] if item["type"] == "string")
    assert old_string["maxLength"] == 256 and new_string["maxLength"] == SEMANTIC_TEXT_LIMIT == 1000
    assert {key: value for key, value in old_string.items() if key != "maxLength"} == {
        key: value for key, value in new_string.items() if key != "maxLength"}
