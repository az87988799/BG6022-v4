"""Real retrieval/reply replay in an isolated Store copy; no live side effects."""

import copy
import hashlib
import json
from datetime import datetime
from pathlib import Path

import pytest
from test_context import payload

from orca_agent.condition_selection import commit_selections, selection_action_parameters
from orca_agent.context import build_context
from orca_agent.model_usage import current_basis
from orca_agent.proposals import ProposalError
from orca_agent.store import Store

FIXTURE = Path(__file__).parents[1] / "fixtures/code_selection_live002"
MANIFEST = json.loads((FIXTURE / "manifest.json").read_bytes())
REPLIES = MANIFEST["replies_in_model_record_order"]


def copied_run(tmp_path):
    store = Store(tmp_path / "data", environment_root=tmp_path / "environment")
    destination = store.path("runs/" + MANIFEST["run_id"])
    for item in MANIFEST["files"]:
        raw = (FIXTURE / item["path"]).read_bytes()
        assert len(raw) == item["bytes"] and hashlib.sha256(raw).hexdigest() == item["sha256"]
        target = destination / item["path"]
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(raw)
    return store, store.load_run(MANIFEST["run_id"])


def parameters(index=-1):
    response = json.loads((FIXTURE / REPLIES[index]["path"]).read_bytes())
    assert json.loads(response["raw_content"]) == response["proposal"]
    return response["proposal"]["parameters"]


def submit(store, run, proposed):
    return commit_selections(store, run, proposed, decision_id="offline_selection",
                             basis=current_basis(store, run))


@pytest.mark.parametrize("index", range(len(REPLIES)))
def test_real_rejected_parameters_keep_precise_field_errors_visible(tmp_path, index):
    store, run = copied_run(tmp_path)
    before = store.load_run(run.id).model_dump_json(), store.load_request(run).model_dump_json()
    proposed = parameters(index)
    with pytest.raises(ProposalError) as rejected:
        submit(store, run, proposed)
    corrections = rejected.value.detail["corrections"]
    errors = {tuple(item["path"]): item["reason"] for item in corrections}
    if index < 2:
        assert errors["parameters", "choices", "method", "sources", 0, "quote"] == "source_quote_not_exact_excerpt"
        basis_path = ("parameters", "choices", "basis", "value" if index == 0 else "sources")
        assert errors[basis_path] == ("selected_value_absent_from_official_quote" if index == 0
                                      else "official_documentation_required")
    else:
        assert errors == {("parameters", "choices", "method", "value"):
                          "selected_value_absent_from_official_quote"}
    assert all("B3LYP" not in item["instruction"] and "def2-SVP" not in item["instruction"]
               for item in corrections)  # Program feedback supplies no recipe or substitute.
    feedback = {"category": "ProposalError", "requirement": rejected.value.detail}
    results = [store.load_result(run.id, identifier) for identifier in run.result_ids]
    # Replay at a recorded time after all retrieved evidence, without extending
    # the archived deadline or depending on the machine's present-day clock.
    replay_now = datetime.fromisoformat(run.model_records[-1]["created_at"])
    assert max(result.created_at for result in results) <= replay_now < run.deadline
    prepared = build_context(store.load_request(run), run, relevant_tools=[],
        results=results,
        action_parameters={"select_conditions": selection_action_parameters(store.load_request(run))},
        feedback={"validation_error": feedback}, now=replay_now)
    assert payload(prepared)["CONTROL"]["validation_error"] == feedback
    assert prepared.input_token_bound <= run.budget.input_tokens
    assert (store.load_run(run.id).model_dump_json(), store.load_request(run).model_dump_json()) == before
    assert run.usage.model_calls == 10 and run.usage.orca_starts_actual == 0 and not run.attempts


def corrected_parameters():
    proposed = parameters()
    # Test-authored exact citation, not a new model response or a chemical recommendation.
    proposed["choices"]["method"]["sources"][0]["quote"] = (
        "A prominent example is the different implementation of the\n"
        "B3LYP hybrid functional within ORCA and Gaussian.")
    for choice in proposed["choices"].values():
        choice["reason"] = "Offline source-boundary verification only; the cited choice is not scientific validation."
    return proposed


def test_real_official_dft_and_basis_quotes_commit_only_in_offline_run_copy(tmp_path):
    store, run = copied_run(tmp_path)
    prior = store.load_request(run)
    frozen = {item["path"]: (FIXTURE / item["path"]).read_bytes() for item in MANIFEST["files"]}
    original_decision = store.path(f"runs/{run.id}/decisions/{run.decisions[0]['id']}.json").read_bytes()
    updated = submit(store, run, corrected_parameters())
    request = store.load_request(updated)
    assert (request.method, request.basis) == ("B3LYP", "def2-SVP")
    assert request.version == prior.version + 1 and request.goals == prior.goals
    assert request.messages == prior.messages and request.original_text == prior.original_text
    assert updated.permission == run.permission and updated.budget == run.budget and updated.usage == run.usage
    assert not updated.attempts and updated.calls == run.calls and updated.result_ids == run.result_ids
    assert updated.model_records == run.model_records
    for field in ("method", "basis"):
        evidence = request.condition_evidence["request." + field]
        assert request.conditions_source[field] == "auto_selected"
        for source in evidence["sources"]:
            result = store.load_result(run.id, source["result_id"])
            excerpt = result.observations["document_excerpt"]
            assert source["quote"] in excerpt["excerpt"] and source["kind"] == "official_document"
            assert source["document_sha256"] == excerpt["document_sha256"]
    assert store.path(f"runs/{run.id}/decisions/{run.decisions[0]['id']}.json").read_bytes() == original_decision
    assert all((FIXTURE / path).read_bytes() == raw for path, raw in frozen.items())


@pytest.mark.parametrize(("mutation", "suffix", "reason"), [
    ("document_hash", ["sources", 0, "document_sha256"], "source_document_hash_mismatch"),
    ("quote", ["sources", 0, "quote"], "source_quote_not_exact_excerpt"),
    ("unknown_result", ["sources", 0, "result_id"], "source_not_in_current_run"),
    ("value", ["value"], "selected_value_absent_from_official_quote"),
    ("excerpt_hash", ["sources", 0, "quote"], "retrieved_excerpt_hash_mismatch"),
])
def test_source_and_value_checks_keep_the_original_thresholds(tmp_path, mutation, suffix, reason):
    store, run = copied_run(tmp_path)
    proposed = corrected_parameters()
    source = proposed["choices"]["method"]["sources"][0]
    if mutation == "document_hash":
        source["document_sha256"] = "0" * 64
    elif mutation == "quote":
        source["quote"] = source["quote"].replace("\n", " ")
    elif mutation == "unknown_result":
        source["result_id"] = "result_not_retrieved"
    elif mutation == "value":
        proposed["choices"]["method"]["value"] = "UncitedMethod"
    else:
        path = store.path(f"runs/{run.id}/results/{source['result_id']}.json")
        result = json.loads(path.read_bytes())
        result["observations"]["document_excerpt"]["excerpt_sha256"] = "0" * 64
        path.write_text(json.dumps(result), encoding="utf-8")
    before = copy.deepcopy(store.load_request(run))
    with pytest.raises(ProposalError) as rejected:
        submit(store, run, proposed)
    assert [(item["path"], item["reason"]) for item in rejected.value.detail["corrections"]] == [
        (["parameters", "choices", "method", *suffix], reason)]
    assert store.load_request(run) == before and run.request_version == 2


@pytest.mark.parametrize(("field", "selected", "quoted"), [
    ("method", "PBE", "PBE0"), ("method", "B3LYP", "B3LYP/G"),
    ("method", "PBE", "modified-PBE"), ("method", "PBE", "PBE–D3"),
    ("basis", "def2-SVP", "ma-def2-SVP"), ("basis", "def2-TZVP", "def2-TZVPP"),
    ("basis", "6-31G", "6-31G*"), ("basis", "6-31G", "6-31G**"),
    ("basis", "6-31G", "6-31G(d,p)"), ("basis", "6-31G(d)", "6-31G(d,p)"),
])
def test_official_quote_cannot_bind_a_prefix_or_different_identifier(tmp_path, field, selected, quoted):
    from tests.unit.test_general_identity_selection import normalized, source

    store, run = normalized(tmp_path)
    reference = source(store, run, excerpt=quoted + " is the identifier in this synthetic source.")
    prior = copy.deepcopy(store.load_request(run))
    before = store.load_run(run.id).model_dump_json()
    proposal = {"request_version": prior.version, "choices": {field: {
        "value": selected, "reason": "Offline boundary probe; not a scientific recommendation.", "sources": [reference]}}}
    with pytest.raises(ProposalError) as rejected:
        submit(store, run, proposal)
    assert [(item["path"], item["reason"]) for item in rejected.value.detail["corrections"]] == [
        (["parameters", "choices", field, "value"], "selected_value_absent_from_official_quote")]
    assert store.load_request(run) == prior and store.load_run(run.id).model_dump_json() == before


@pytest.mark.parametrize(("field", "selected", "quote"), [
    ("method", "PBE", "PBE0 combines PBE and exact exchange."),
    ("method", "PBE", "The method (PBE) is named independently."),
    ("method", "B3LYP/G", "Distinct identifier: B3LYP/G."),
    ("basis", "6-31G(d,p)", "Use 6-31G(d,p), with its complete suffix."),
    ("basis", "def2-SV(P)", "The name is def2-SV(P)."),
    ("basis", "6-31G**", "Basis: 6-31G**; a distinct identifier."),
    ("basis", "def2-SVP", "(def2-SVP), a complete name."),
    ("method", "PBE0", "The identifier pbe0: case-insensitive spelling."),
])
def test_complete_identifiers_and_sentence_punctuation_preserve_exact_quote(tmp_path, field, selected, quote):
    from tests.unit.test_general_identity_selection import normalized, source

    store, run = normalized(tmp_path)
    reference = source(store, run, excerpt=quote)
    prior = store.load_request(run)
    permission, budget, usage = copy.deepcopy((run.permission, run.budget, run.usage))
    proposal = {"request_version": prior.version, "choices": {field: {
        "value": selected, "reason": "Offline boundary probe; not a scientific recommendation.", "sources": [reference]}}}
    updated = submit(store, run, proposal)
    request = store.load_request(updated)
    assert request.conditions[field] == selected and request.conditions_source[field] == "auto_selected"
    evidence = request.condition_evidence["request." + field]["sources"][0]
    assert evidence["quote"] == quote and evidence["document_sha256"] == reference["document_sha256"]
    assert request.goals == prior.goals and request.messages == prior.messages
    assert (updated.permission, updated.budget, updated.usage) == (permission, budget, usage)
