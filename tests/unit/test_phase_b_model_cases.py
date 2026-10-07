"""Offline input/grader contract checks; none count as live-model evaluations."""

import copy
import importlib.util
import json
from pathlib import Path

import pytest
from test_agent import ScriptedTransport

from orca_agent import agent, runner
from orca_agent.config import Config
from orca_agent.context import build_context
from orca_agent.goals import validate_goal_evidence
from orca_agent.llm import ModelReply, ModelUsage
from orca_agent.model_usage import current_basis, send_model
from orca_agent.models import OutputBinding, Plan, Step
from orca_agent.store import Store, StoreError, sha256_file
from orca_agent.tools.dispatch import execute_call

PROJECT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("phase_b_model_cases", PROJECT / "tests/helpers/phase_b_model_cases.py")
CASES = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(CASES)


@pytest.fixture
def store(tmp_path):
    return Store(tmp_path / "data", environment_root=tmp_path / "environment")


def test_exact_frozen_allocation_no_implicit_expansion():
    ids = CASES.fixed_variant_ids()
    assert len(ids) == len(set(ids)) == 25
    document = json.loads(CASES.CASES.read_text(encoding="utf-8"))
    assert ids == tuple(document["batch_budget"]["model_allocation"]["fixed_evidence"]["variant_ids"])
    with pytest.raises(ValueError, match="outside"):
        CASES.variant_spec("V-04/left")


@pytest.mark.parametrize("variant", CASES.fixed_variant_ids())
def test_every_input_is_no_science_no_calls_and_bounded(store, variant):
    run, metadata = CASES.create_request(store, variant, 1)
    request = store.load_request(run)
    assert run.agent_enabled and run.batch_category == "formal"
    assert not run.permission.scientific_execution
    assert run.budget.orca_starts == run.budget.extra_orca_starts == 0
    assert run.budget.model_calls == 4 and run.budget.model_tokens == 32000
    assert not run.attempts and not run.model_records
    assert len(run.calls) == int(variant == "V-07/instructions-in-file")
    assert not store.path(f"runs/{run.id}/environment.json").exists()
    prepared = build_context(request, run, relevant_tools=run.permission.allowed_tools,
                             results=[store.load_result(run.id, rid) for rid in run.result_ids])
    assert prepared.input_token_bound <= 12000
    body = prepared.canonical_body
    for forbidden in (variant, "expected_ref", "independent_energy_eh", "fixture_gaps", "reference-review"):
        assert forbidden not in body
    assert "sampling-left" not in body
    assert metadata["expected"] == CASES.variant_spec(variant)["variant"]["expected"]
    grade = CASES.evaluate_response(store, run, metadata)
    assert grade["status"] == "not_verified"
    assert not grade["real_model_evidence_present"]
    assert grade["safety_invariants_passed"]
    assert all(axis["status"] == "not_verified" for axis in grade["explanation"].values())


@pytest.mark.parametrize("value", [0, 4, -1, True, "1", 1.0])
def test_repetition_is_frozen_not_an_unbounded_new_budget(store, value):
    with pytest.raises(ValueError, match="repetition"):
        CASES.create_request(store, "V-07/discover-and-read", value)


def test_three_repetitions_get_independent_identity_but_same_contract(store):
    prepared = [CASES.create_request(store, "V-01/allowed-default-origin", n) for n in (1, 2, 3)]
    assert len({run.id for run, _ in prepared}) == 3
    assert len({metadata["spec_sha256"] for _, metadata in prepared}) == 1
    for run, _ in prepared:
        request = store.load_request(run)
        assert request.conditions_source["method"] == request.conditions_source["basis"] == "default"
        assert request.conditions["allowed_defaults"]["source"] == "explicit_user_permission"


def test_ambiguous_conflicting_and_unsupported_purposes_are_preserved(store):
    run, _ = CASES.create_request(store, "V-01/ambiguous-pronoun", 1)
    request = store.load_request(run)
    assert request.geometry_artifact_id is None and request.method is None
    assert "geometry_reference" in request.unresolved
    run, _ = CASES.create_request(store, "V-01/conflicting-conditions", 1)
    request = store.load_request(run)
    assert request.charge is None
    assert request.conditions["conflicting_user_charge_assertions"] == [0, 1]
    run, _ = CASES.create_request(store, "V-01/unsupported-spectrum", 1)
    request = store.load_request(run)
    assert request.goals[0].port == "infrared_spectrum"
    assert request.conditions["temperature_k"] == 298.15


def test_multi_turn_cannot_skip_clarification_or_clear_original_goals(store):
    run, metadata = CASES.create_request(store, "V-01/multi-turn", 1)
    assert len(store.load_request(run).messages) == 1
    assert store.load_request(run).method is None
    with pytest.raises(StoreError, match="clarification"):
        CASES.advance_user_turn(store, run, metadata)
    # Local synthetic decision tests the user-update protocol only, not a model.
    run.state = "waiting_user"
    run.decisions.append({"id": "offline_first", "action": "clarify", "reason": "need geometry",
                          "parameters": {"questions": ["Which geometry?"], "unresolved": ["geometry_artifact_id"]},
                          "basis": current_basis(store, run)})
    store.save_run(run)
    run = CASES.advance_user_turn(store, run, metadata)
    assert len(store.load_request(run).messages) == 2
    assert store.load_request(run).geometry_artifact_id
    assert store.load_request(run).method is None
    run.state = "waiting_user"
    store.save_run(run)
    with pytest.raises(StoreError, match="clarification"):
        CASES.advance_user_turn(store, run, metadata)
    run.decisions.append({"id": "offline_second", "action": "clarify", "reason": "need conditions",
                          "parameters": {"questions": ["Which scientific conditions?"],
                                         "unresolved": ["method", "basis", "charge", "multiplicity"]},
                          "basis": current_basis(store, run)})
    store.save_run(run)
    run = CASES.advance_user_turn(store, run, metadata)
    request = store.load_request(run)
    assert len(request.messages) == 3
    assert request.method == "HF" and not request.unresolved
    assert request.goals[0].conditions == metadata["initial_request"]["goals"][0]["conditions"]
    assert not run.attempts and run.usage.model_calls == 0


def test_missing_reference_store_keeps_explicit_gaps_never_fabricates(store):
    run, metadata = CASES.create_request(store, "V-09/compatible-water", 1)
    assert len(metadata["fixture_gaps"]) == 2
    assert not metadata["references"]
    assert not store.load_request(run).conditions["available_evidence"]
    assert len(list((store.root / "runs").iterdir())) == 1
    goals = store.load_request(run).goals
    assert {g.port for g in goals} == {"energy_difference", "member_table"}
    assert goals[1].conditions["analysis_goal_id"] == goals[0].id


def test_free_energy_never_substituted_by_electronic_energy(store):
    run, _ = CASES.create_request(store, "V-02/free-energy-protected", 1)
    request = store.load_request(run)
    free, electronic = request.goals
    assert free.required and electronic.required
    assert free.port == "gibbs_free_energy_difference"
    assert free.conditions["temperature_k"] == 298.15
    assert free.conditions["standard_pressure_atm"] == 1
    assert "thermal_evidence_missing" in free.unresolved
    assert electronic.port == "energy_difference"


def test_real_raw_value_grading_does_not_publish_scientific_dipole(store):
    run, metadata = CASES.create_request(store, "V-07/discover-and-read", 1)
    request = store.load_request(run)
    goal = request.goals[0]
    result = execute_call(store, run, "evidence.value", goal.conditions["query"])
    assert result.operation_status == "completed"
    assert not result.qualified_outputs
    agent._bind_query_goals(store, run, result)
    assert not runner._goals(store, run, None, {})
    store.save_run(run)
    grade = CASES.evaluate_response(store, run, metadata)
    values = {item["metric"]: item for item in grade["assertions"]}
    assert values["read.value_equals_raw_field"]["observed"] is True
    assert values["scientific_dipole_port.published"]["observed"] is False
    assert values["query_goal.satisfied"]["observed"] is False
    assert grade["status"] == "not_verified"


def test_no_orca_scope_uses_bounded_root_discovery_while_full_value_still_fails(store):
    from orca_agent.tools.evidence import read_value

    run, metadata = CASES.create_request(store, "V-10/no-orca-installed", 1)
    request = store.load_request(run)
    artifact = metadata["query_artifact_id"]
    before = sha256_file(store.artifact_path(artifact))
    assert len(request.goals) == 1
    goal = request.goals[0]
    assert goal.id == "artifact_presence_and_raw_observations"
    assert goal.port == "content_index" and goal.required
    assert goal.conditions["query"] == {"artifact_id": artifact, "path": []}
    assert "root-key content index" in metadata["tested_scope"][0]
    assert {"evidence.list", "evidence.discover"} <= set(run.permission.allowed_tools)
    with pytest.raises(ValueError, match="200 elements"):
        read_value(store, artifact, [])
    result = execute_call(store, run, "evidence.discover", goal.conditions["query"])
    assert result.operation_status == "completed"
    observed = result.observations["content_index"]
    assert observed["status"] == "observed" and observed["path"] == []
    assert observed["entries"] and observed["scientific_status"] == "unverified"
    agent._bind_query_goals(store, run, result)
    assert runner._goals(store, run, None, {})
    assert not result.qualified_outputs and not run.attempts
    assert run.usage.orca_starts_actual == run.usage.postprocess_starts == 0
    assert not store.path(f"runs/{run.id}/environment.json").exists()
    assert sha256_file(store.artifact_path(artifact)) == before


@pytest.mark.parametrize("variant", ["discover-and-read", "array-location", "instructions-in-file"])
def test_v07_all_requested_reads_precede_completion_within_frozen_model_budget(store, variant):
    run, metadata = CASES.create_request(store, "V-07/" + variant, 1)
    request = store.load_request(run)
    goals = {goal.id: goal for goal in request.goals}
    sequence = request.conditions["user_query_sequence"]
    expected = ["raw_field_discovery", "raw_geometry_slice", "raw_field_observation"] if variant == "array-location" else [
        "raw_field_discovery", "raw_field_observation"]
    assert sequence == expected and all(goal.required for goal in goals.values())
    assert goals["raw_field_discovery"].port == "content_index"
    assert goals["raw_field_discovery"].conditions["query"] == {
        "artifact_id": metadata["query_artifact_id"]}

    def read(goal_id, prior):
        def proposal(data):
            status = data["AUTHORITY"]["goal_status"]
            assert all(status[key] == "satisfied" for key in prior)
            assert status[goal_id] == "insufficient_evidence"
            current = next(g for g in data["AUTHORITY"]["request"]["goals"] if g["id"] == goal_id)
            return {"action": "call_tool", "parameters": {
                "tool": "evidence.discover" if current["port"] == "content_index" else "evidence.value",
                "parameters": current["conditions"]["query"]}}
        return proposal

    def stop(data):
        assert all(value == "satisfied" for value in data["AUTHORITY"]["goal_status"].values())
        return {"action": "stop", "parameters": {},
                "reason": "Raw fields and the requested JSON values were read; no scientific dipole is qualified."}

    class OfflineBatch:
        def reserve_model(self, *args):
            pass

        def settle_model(self, *args):
            pass

    transport = ScriptedTransport(*(read(key, sequence[:i]) for i, key in enumerate(sequence)), stop)
    completed = agent.execute(store, Config(data_root=store.root), run.id,
                              transport=transport, batch=OfflineBatch())
    assert completed.state == "completed", completed.diagnostics
    assert completed.usage.model_calls == len(sequence) + 1 <= completed.budget.model_calls == 4
    assert completed.usage.evidence_reads == len(sequence) + int(variant == "instructions-in-file")
    assert not completed.attempts and completed.usage.orca_starts_actual == 0
    assert all(not store.load_result(run.id, rid).qualified_outputs for rid in completed.result_ids)
    assertions = {item["metric"]: item for item in CASES.evaluate_response(store, completed, metadata)["assertions"]}
    if variant == "array-location":
        assert goals["raw_geometry_slice"].conditions["query"]["path"] == [
            {"kind": "key", "key": "Geometries"}, {"kind": "slice", "start": 0, "stop": 1}]
        kinds = {part["kind"] for call in completed.calls for part in call.parameters.get("path", [])}
        assert kinds == {"key", "index", "slice"}
        assert assertions["query.typed_key_index_slice"]["observed"]
        assert assertions["observation.geometry_index"]["observed"] == 0
    if variant == "discover-and-read":
        assert assertions["query_goal.satisfied"]["observed"]


def test_injected_instructions_are_separate_from_unchanged_real_json(store):
    run, metadata = CASES.create_request(store, "V-07/instructions-in-file", 1)
    request = store.load_request(run)
    files = request.conditions["registered_evidence"]
    raw = store.load_artifact(files["property_json"])
    note = store.load_artifact(files["untrusted_note"])
    assert raw.sha256 == sha256_file(CASES.RAW / "job.property.json")
    assert note.source["kind"] == "synthetic_prompt_injection_sidecar"
    assert "Python" in store.artifact_path(note.id).read_text(encoding="utf-8")
    assert "evidence.import" not in run.permission.allowed_tools
    assert not run.permission.artifact_writes
    assert metadata["prepared_inputs"][0]["kind"] == "synthetic_prompt_injection_sidecar"
    assert run.usage.evidence_reads == 1
    result = store.load_result(run.id, metadata["preparation_result_ids"][0])
    assert result.observations["text_window"]["scientific_status"] == "unverified"
    context = json.loads(build_context(request, run, results=[result]).body()["messages"][1]["content"])
    assert "Python" not in json.dumps(context["AUTHORITY"], ensure_ascii=False)
    assert "Python" in json.dumps(context["DATA"], ensure_ascii=False)


def test_stdout_excerpt_is_actual_energy_line_without_conditions_or_attempt(store):
    run, metadata = CASES.create_request(store, "V-08/missing-conditions", 1)
    source = next(iter(store._read_json(f"runs/{run.id}/sources.json").values()))
    excerpt = Path(source["files"][0]["path"]).read_bytes()
    original = (CASES.RAW / "stdout.out").read_bytes()
    assert excerpt and all(line in original for line in excerpt.splitlines())
    assert b"FINAL SINGLE POINT ENERGY" in excerpt
    assert b"STO-3G" not in excerpt and b"RHF" not in excerpt
    assert not run.attempts and metadata["prepared_inputs"][0]["kind"] == "exact_archived_excerpt"
    assert not Path(source["files"][1]["path"]).exists()
    assert not Path(source["files"][2]["path"]).exists()


@pytest.mark.parametrize("variant", ["stdout-only", "missing-conditions"])
def test_import_uses_production_tool_keeps_missing_files_and_unknown_history(store, variant):
    run, metadata = CASES.create_request(store, "V-08/" + variant, 1)
    request = store.load_request(run)
    source_id = request.conditions["registered_source_id"]
    step = Step(id="import_step", logical_id="import_step", tool="evidence.import", parameters={"source_id": source_id})
    inventory, excerpt = request.goals
    assert inventory.conditions["accept_partial_observations"]
    assert excerpt.conditions == {"quantity": "imported_stdout_energy_line",
        "imported_source_id": source_id, "require_nonempty_matches": True,
        "query": {"query": "FINAL SINGLE POINT ENERGY"}}
    plan = Plan(request_id=request.id, steps=[step], goal_map={inventory.id:
        OutputBinding(step_id=step.id, port="imported_evidence"), excerpt.id:
        OutputBinding(gap="The import must first create the immutable stdout snapshot.", port="search_hits")})
    run = store.commit_revision(run, plan, decision_id="offline_plan", basis=current_basis(store, run))
    result = execute_call(store, run, step.tool, step.parameters, step=step)
    observation = result.observations["imported_evidence"]
    assert observation["status"] == "partial"
    assert observation["conditions"] == "unknown"
    assert validate_goal_evidence(store, run, request, inventory, result)
    assert not validate_goal_evidence(store, run, request, excerpt, result)
    assert {item["file"] for item in observation["missing"]} == {"job.inp", "job.property.json"}
    for artifact_id in result.artifact_ids:
        artifact = store.load_artifact(artifact_id)
        assert artifact.source["original_attempt"] == artifact.source["original_cost"] == "unknown"
    assert not result.qualified_outputs and not run.attempts
    grade = CASES.evaluate_response(store, run, metadata)
    assertions = {item["metric"]: item for item in grade["assertions"]}
    if variant == "stdout-only":
        assert assertions["missing_files.includes_input_json"]["observed"]
        assert not assertions["original_run.fabricated"]["observed"]
        assert not assertions["available_observations.returned"]["observed"]
    else:
        assert assertions["necessary_conditions.status"]["observed"] == "unknown"
    # Locate only the search window here; the actual excerpt comes from the
    # production read-only Tool and stays unqualified raw evidence.
    artifact_id = observation["artifact_ids"][0]
    lines = store.artifact_path(artifact_id).read_bytes().split(b"\n")
    line = next(i for i, text in enumerate(lines, 1) if b"FINAL SINGLE POINT ENERGY" in text)
    hint = request.conditions["source_read_hint"]
    assert hint["source_sha256"] == store.load_artifact(artifact_id).sha256
    assert hint["start_line"] <= line < hint["start_line"] + hint["max_lines"]
    assert 1 <= hint["max_lines"] <= 200 and "-74." not in json.dumps(hint)
    observed = execute_call(store, run, "evidence.search", {"artifact_id": artifact_id,
        "query": hint["query"], "start_line": hint["start_line"], "max_lines": hint["max_lines"]})
    assert validate_goal_evidence(store, run, request, excerpt, observed)
    assert observed.observations["search_hits"]["matches"][0] == {
        "line": line, "text": lines[line - 1].decode().rstrip("\r\n")}
    assert not observed.qualified_outputs and not run.attempts
    assert run.usage.evidence_reads == 2
    if variant == "stdout-only":
        graded = CASES.evaluate_response(store, run, metadata)
        assert next(a for a in graded["assertions"] if a["metric"] == "available_observations.returned")["observed"]


def test_grader_rejects_changed_assertions_and_unquoted_reviews(store):
    run, metadata = CASES.create_request(store, "V-01/allowed-default-origin", 1)
    changed = copy.deepcopy(metadata)
    changed["expected"][0]["value"] = "inferred"
    with pytest.raises(ValueError, match="frozen"):
        CASES.evaluate_response(store, run, changed)
    with pytest.raises(ValueError, match="exact model text"):
        CASES.evaluate_response(store, run, metadata, review={"explanation": {
            "quantity": {"passed": True, "quote": "fabricated quote", "rationale": "says energy"}}})


@pytest.mark.parametrize("variant", ["stdout-only", "missing-conditions"])
def test_import_then_actual_stdout_read_and_explanation_fit_three_offline_rounds(store, variant):
    run, metadata = CASES.create_request(store, "V-08/" + variant, 1)

    def plan(data):
        goals = data["AUTHORITY"]["request"]["goals"]
        return {"action": "initial_plan", "parameters": {
            "steps": [{"key": "import", "tool": "evidence.import",
                       "parameters": goals[0]["conditions"]["query"]}],
            "goal_map": {goals[0]["id"]: {"step_key": "import", "port": "imported_evidence"},
                         goals[1]["id"]: {"gap": "Need imported stdout snapshot", "port": "search_hits"}}}}

    def read(data):
        assert list(data["AUTHORITY"]["goal_status"].values()) == ["satisfied", "insufficient_evidence"]
        imported = data["DATA"]["results"][0]["unqualified_observations"]["imported_evidence"]
        hint = data["AUTHORITY"]["request"]["conditions"]["source_read_hint"]
        assert imported["conditions"] == "unknown" and imported["status"] == "partial"
        return {"action": "call_tool", "parameters": {"tool": "evidence.search", "parameters": {
            "artifact_id": imported["artifact_ids"][0], "query": hint["query"],
            "start_line": hint["start_line"], "max_lines": hint["max_lines"]}}}

    def stop(data):
        assert all(value == "satisfied" for value in data["AUTHORITY"]["goal_status"].values())
        observed = next(result["unqualified_observations"]["search_hits"] for result in data["DATA"]["results"]
                        if "search_hits" in result["unqualified_observations"])
        assert observed["matches"] and observed["scientific_status"] == "unverified"
        return {"action": "stop", "parameters": {},
                "reason": "The saved stdout energy line was read. Conditions and historical cost remain unknown."}

    class OfflineBatch:
        def reserve_model(self, *args):
            pass

        def settle_model(self, *args):
            pass

    transport = ScriptedTransport(plan, read, stop)
    completed = agent.execute(store, Config(data_root=store.root), run.id,
                              transport=transport, batch=OfflineBatch())
    assert completed.state == "completed", completed.diagnostics
    assert completed.usage.model_calls == len(transport.sent) == 3
    assert completed.usage.evidence_reads == 2 and not completed.attempts
    assert completed.usage.analysis_executions == completed.usage.orca_starts_actual == 0
    assert CASES.evaluate_response(store, completed, metadata)["status"] == "not_verified"


def test_review_can_grade_explanation_but_never_fabricate_live_evidence(store):
    run, metadata = CASES.create_request(store, "V-01/allowed-default-origin", 1)
    run.decisions.append({"id": "offline", "action": "stop", "reason": "Explicit defaults; energy is unknown.",
                          "parameters": {"reason": "No calculation permitted."}, "basis": current_basis(store, run)})
    store.save_run(run)
    reviewed = {"passed": True, "quote": "Explicit defaults", "rationale": "offline grading contract only"}
    review = {"explanation": dict.fromkeys(CASES.EXPLANATION_AXES, reviewed),
              "behavior": {"default_disclosed": reviewed}}
    grade = CASES.evaluate_response(store, run, metadata, review=review)
    assert all(axis["status"] == "passed" for axis in grade["explanation"].values())
    assert grade["status"] == "not_verified" and not grade["real_model_evidence_present"]


@pytest.mark.parametrize("fields,expected", [
    ({"all_proposal_facts_passed": True, "semantic_review_passed": True}, "passed"),
    ({"all_proposal_facts_passed": False, "semantic_review_passed": True}, "failed"),
    ({"all_proposal_facts_passed": True, "semantic_review_passed": False}, "failed"),
    ({"all_proposal_facts_passed": False, "semantic_review_passed": False}, "failed"),
    ({"all_proposal_facts_passed": False}, "failed"),
    ({"semantic_review_passed": False}, "failed"),
    ({}, "not_verified"),
    ({"all_proposal_facts_passed": True}, "not_verified"),
    ({"semantic_review_passed": True}, "not_verified"),
    ({"all_proposal_facts_passed": 1, "semantic_review_passed": "true"}, "not_verified"),
])
def test_six_axes_cannot_override_failed_or_missing_proposal_review(store, monkeypatch, fields, expected):
    from tests.helpers import phase_b_grading
    run, metadata = CASES.create_request(store, "V-01/allowed-default-origin", 1)
    reason = "Explicit defaults; offline final explanation fixture."
    # Isolated synthetic metadata exercises the overall success branch without
    # sending a request or presenting this test as real-model evidence. Original
    # proposal reads are stubbed explicitly; the live driver is never invoked.
    run.model_records = [{"id": "offline-grader-boundary", "status": "known", "model": "deepseek-flash",
                          "sdk_version": "2.28.0", "response_model": "synthetic-unit-grader-branch"}]
    store.save_run(run)
    monkeypatch.setattr(CASES, "_actions", lambda *_: [{"action": "stop", "reason": reason, "parameters": {}}])
    monkeypatch.setattr(phase_b_grading, "model_response_evidence", lambda *_: {
        "present": True, "offline_fixture_only": True})
    entry = {"passed": True, "quote": "Explicit defaults", "rationale": "Synthetic final-axis fixture only."}
    review = {"explanation": dict.fromkeys(CASES.EXPLANATION_AXES, entry),
              "behavior": {"default_disclosed": entry}, **fields,
              "all_proposal_review": [{"model_id": "offline-intermediate", "quote": "Three SPs completed",
                  "rationale": "Synthetic intermediate claim exceeded the two actually observed Results."}]}
    grade = CASES.evaluate_response(store, run, metadata, review=review)
    assert all(axis["status"] == "passed" for axis in grade["explanation"].values())
    assert all(item["status"] == "passed" for item in grade["assertions"])
    assert grade["proposal_review"]["status"] == expected
    assert grade["status"] == ("passed" if expected == "passed" else "incomplete_or_failed")
    for key in ("all_proposal_facts_passed", "semantic_review_passed"):
        assert grade["proposal_review"][key] is (fields.get(key) if type(fields.get(key)) is bool else None)
    assert run.usage.model_calls == 0 and not run.attempts


@pytest.mark.parametrize("final_action,expected", [(None, False), ("rejected", False), ("stop", True)])
def test_result_explanation_gate_rejects_unaccepted_stop_despite_read_success(store, final_action, expected):
    run, metadata = CASES.create_request(store, "V-07/discover-and-read", 1)
    request = store.load_request(run)
    query = next(goal.conditions["query"] for goal in request.goals if goal.port == "value_observation")
    execute_call(store, run, "evidence.value", query)
    if final_action:
        run.decisions.append({"id": "offline-explicit-delivery-test", "action": final_action,
            "reason": "Offline gate fixture, not a real model response.", "parameters": {},
            "basis": current_basis(store, run)})
        run.state = "failed"
        store.save_run(run)
    grade = CASES.evaluate_response(store, run, metadata)
    gate = grade["required_final_response_accepted"]
    assert gate["required"] is True and gate["passed"] is expected
    assert gate["status"] == ("passed" if expected else "failed")
    assert grade["status"] == "not_verified" and not grade["real_model_evidence_present"]


@pytest.mark.parametrize("state,goals_complete,expected", [
    ("waiting_user", False, True), ("failed", False, False), ("waiting_user", True, False)])
def test_result_explanation_gate_accepts_clarification_only_for_waiting_unmet_goals(
        store, state, goals_complete, expected):
    run, metadata = CASES.create_request(store, "V-07/discover-and-read", 1)
    request = store.load_request(run)
    query = next(goal.conditions["query"] for goal in request.goals if goal.port == "value_observation")
    execute_call(store, run, "evidence.value", query)
    run.decisions.append({"id": "offline-post-result-clarification", "action": "clarify",
        "reason": "Explicit synthetic post-result clarification gate fixture.",
        "parameters": {"questions": ["Which missing condition applies?"], "unresolved": ["condition"]},
        "basis": current_basis(store, run)})
    run.state = state
    if goals_complete:
        run.goal_status = {goal.id: "satisfied" for goal in request.goals}
    store.save_run(run)
    gate = CASES.evaluate_response(store, run, metadata)["required_final_response_accepted"]
    assert gate["required"] is True and gate["passed"] is expected
    assert gate["accepted_action"] == ("clarify" if expected else None)


def test_clarification_without_any_tool_result_does_not_require_stop(store):
    run, metadata = CASES.create_request(store, "V-01/synonym-single-point", 1)
    run.decisions.append({"id": "offline-clarification-gate", "action": "clarify",
        "reason": "No execution is permitted; offline test only.", "parameters": {},
        "basis": current_basis(store, run)})
    store.save_run(run)
    gate = CASES.evaluate_response(store, run, metadata)["required_final_response_accepted"]
    assert gate["required"] is False and gate["passed"] is None and gate["status"] == "not_applicable"


@pytest.mark.parametrize("variant", ["V-02/free-energy-protected", "V-09/compatible-water",
                                      "V-06/insufficient-additional-budget"])
def test_existing_real_references_fit_context_without_writing_historical_runs(store, variant):
    archive_root = PROJECT / "data/phase-b/reference"
    index = CASES._read(CASES.INDEX)["records"]
    if not (archive_root / "runs" / index["sampling-left-center"]["run_id"] / "run.json").exists():
        pytest.skip("local real reference archive unavailable; source-backed context unverified")
    archive = Store(archive_root)
    run, metadata = CASES.create_request(store, variant, 1)
    request = store.load_request(run)
    before = {item["run_id"]: sha256_file(archive.path(f"runs/{item['run_id']}/run.json"))
              for item in index.values()}
    if variant.startswith("V-06"):
        manifest = CASES._read(CASES.MANIFEST)["windows"][0]
        pairs = [(c["model_candidate_id"], c["reference_id"]) for c in manifest["candidates"] if c["required_initial"]]
    else:
        pairs = [("A", "sampling-left-center"), ("B", "sampling-left-minus_half")]
    # Only local context objects change. The original scientific archive is read
    # through the production hash/check binding and is never copied or rewritten.
    bound = {key: CASES._reference(archive, ref, metadata) for key, ref in pairs}
    assert all(bound.values())
    request.conditions["available_evidence"] = bound
    run.permission.result_ids = [v["binding"]["result_id"] for v in bound.values()]
    run.permission.artifact_ids = list(dict.fromkeys(metadata["artifact_ids"]))
    prepared = build_context(request, run, relevant_tools=run.permission.allowed_tools)
    assert prepared.input_token_bound <= 12000
    assert "sampling-left" not in prepared.canonical_body
    for run_id, digest in before.items():
        assert sha256_file(archive.path(f"runs/{run_id}/run.json")) == digest


def test_grader_reads_actual_persisted_model_reply_tuple_without_resend(store):
    run, metadata = CASES.create_request(store, "V-01/allowed-default-origin", 1)
    prepared = build_context(store.load_request(run), run)
    basis = current_basis(store, run)
    proposal = {**basis, "action": "stop", "parameters": {}, "related_results": [],
                "reason": "Explicit defaults; electronic energy is unknown without a permitted calculation."}

    class OfflineTransport:
        sends = 0

        def send(self, request, *, reserve, settle):
            self.sends += 1
            ticket = reserve(request)
            reply = ModelReply(request_hash=request.request_hash, proposal=proposal,
                               usage=ModelUsage(10, 5, 15), response_model="offline-fake")
            settle(ticket, reply)
            return reply

    class OfflineBatch:
        def reserve_model(self, *args):
            pass

        def settle_model(self, *args):
            pass

    transport = OfflineTransport()
    send_model(store, run, prepared, transport, basis=basis, logical_id="offline_decision", batch=OfflineBatch())
    run.decisions.append({"id": run.model_records[0]["id"], "basis": basis, "action": "stop"})
    store.save_run(run)
    review = {"behavior": {"default_disclosed": {"passed": True, "quote": "Explicit defaults",
                                                 "rationale": "exact persisted Proposal explanation"}}}
    first = CASES.evaluate_response(store, run, metadata, review=review)
    second = CASES.evaluate_response(store, run, metadata, review=review)
    assert first == second and transport.sends == 1
    assert first["status"] == "not_verified" and not first["real_model_evidence_present"]
    assert next(a for a in first["assertions"] if a["metric"] == "default_disclosed")["status"] == "passed"


def test_counterexamples_keep_declared_conditions_distinct_from_actual_sources(store):
    run, metadata = CASES.create_request(store, "V-09/different-method", 1)
    request = store.load_request(run)
    assert request.systems[1].conditions["basis"] == "6-31G"
    assert "not a fabricated 6-31G" in metadata["tested_scope"][0]
    run, metadata = CASES.create_request(store, "V-09/missing-electron-state", 1)
    request = store.load_request(run)
    assert request.systems[1].conditions["multiplicity"] is None
    assert "source applicability must not be assumed" in metadata["tested_scope"][0]


def test_legacy_archive_restoration_preserves_original_bytes_and_rule(store):
    run, metadata = CASES.create_request(store, "V-09/unqualified-or-old-rule", 1)
    if not metadata["archive_imports"]:
        pytest.skip("phase-A original archive unavailable; immutable restoration unverified")
    archived = metadata["archive_imports"][0]
    assert archived["rule_versions"] == ["orca-hf-1"]
    source = store.load_request(run).conditions["available_evidence"]["B"]
    assert source["observed_historical_output"]["scientific_status"] == "not_verified_for_current_rule"
    assert "qualified" not in source and source["binding"]["result_id"] in run.permission.result_ids
    for member in archived["manifest"]:
        assert sha256_file(Path(archived["source_root"]) / member["relative_path"]) == member["sha256"]
        assert sha256_file(store.path(member["relative_path"])) == member["sha256"]
    assert not run.attempts and run.usage.orca_starts_actual == 0


@pytest.mark.parametrize("case", ["water_sp", "methane_opt"])
def test_readable_foreign_checkout_archive_is_a_gap_without_consumption(store, tmp_path, monkeypatch, case):
    checkout, foreign = tmp_path / "relocated-checkout", tmp_path / "other-checkout/data"
    foreign.mkdir(parents=True)
    receipt = foreign / "receipt.json"
    receipt.write_text('{"offline_boundary_test": true}', encoding="utf-8")
    index = checkout / "docs/acceptance/phase-a/evidence-index.json"
    index.parent.mkdir(parents=True)
    index.write_text(json.dumps({"current_cases": {case: {
        "receipt_path": str(receipt), "receipt_sha256": sha256_file(receipt),
        "receipt": {"store_root": str(foreign)}}}}), encoding="utf-8")
    monkeypatch.setattr(CASES, "PROJECT", checkout)
    monkeypatch.setattr(CASES, "Store", lambda *_: pytest.fail("foreign Store must not be opened"))
    monkeypatch.setattr(CASES, "sha256_file", lambda *_: pytest.fail("foreign receipt must not be consumed"))
    metadata = {"fixture_gaps": [], "archive_imports": [], "artifact_ids": []}
    assert receipt.is_file()
    assert CASES._legacy_reference(store, case, metadata) is None
    assert metadata == {"fixture_gaps": [{"kind": "legacy_archive_not_restored_for_checkout", "case": case}],
                        "archive_imports": [], "artifact_ids": []}
    assert not list(store.root.rglob("*.json"))
