"""Context facts stay scoped and truthful; offline replay is not a model rerun."""

import copy
import json
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from orca_agent.applicability import effective_conditions
from orca_agent.config import Config
from orca_agent.context import build_context
from orca_agent.models import Goal, Result, SystemInput
from orca_agent.proposals import call_tool_instruction, call_tool_parameters_schema
from orca_agent.semantic import action_parameters
from orca_agent.store import Store, sha256_file
from orca_agent.tools.registry import get_tool
from tests.unit.test_context import objects, payload

_HISTORICAL_DEFAULTS = {
    "permission": {"code_execution": False},
    "budget_limits": {"logical_steps": 12, "knowledge_queries": 0,
                      "internal_work_units": 0, "code_retries": 0, "artifact_count": 200,
                      "artifact_bytes": 268435456, "artifact_total_bytes": 268435456},
    "request": {"automatic_selection": [], "selection_reasons": {}},
    "goal": {"evidence_contract_version": "legacy-1", "verification": "qualified",
             "quantity": None, "minimum_evidence_source": {}},
}


def assert_historical_projection(actual, archived, section):
    """Preserve every recorded fact; allow only explicit, conservative read defaults.

    ADR 0031 permits interpreting missing fields, never rewriting old JSON or
    granting code permission. This checks exact values, not arbitrary new keys.
    """
    expected = copy.deepcopy(archived)
    for key, default in _HISTORICAL_DEFAULTS[section].items():
        expected.setdefault(key, copy.deepcopy(default))
    if section == "request":
        assert len(actual["goals"]) == len(expected["goals"])
        for new, old in zip(actual["goals"], expected["goals"], strict=True):
            assert_historical_projection(new, old, "goal")
        expected["goals"] = copy.deepcopy(actual["goals"])
    assert actual == expected


@pytest.mark.parametrize("section,archive,change", [
    ("permission", {"allowed_tools": ["evidence.field"]}, {"code_execution": True}),
    ("permission", {"allowed_tools": ["evidence.field"]}, {"allowed_tools": ["python.execute"]}),
    ("permission", {"version": 1}, {"version": 2}),
    ("permission", {}, {"unknown_permission": False}),
    ("budget_limits", {"orca_starts": 0}, {"orca_starts": 1}),
    ("budget_limits", {}, {"internal_work_units": 1}),
    ("budget_limits", {}, {"artifact_bytes": 268435457}),
    ("request", {"goals": []}, {"automatic_selection": ["method"]}),
    ("goal", {"minimum_check_version": "orca-hf-1"}, {"verification": "reported_with_limitations"}),
    ("goal", {"minimum_check_version": "orca-hf-1"}, {"minimum_check_version": "orca-hf-2"}),
])
def test_history_comparison_rejects_new_authority_and_unknown_defaults(section, archive, change):
    compatible = {**copy.deepcopy(_HISTORICAL_DEFAULTS[section]), **copy.deepcopy(archive)}
    assert_historical_projection(compatible, archive, section)
    with pytest.raises(AssertionError):
        assert_historical_projection({**compatible, **change}, archive, section)


@pytest.mark.parametrize("field,value,general,effective", [
    ("basis", "6-31G", "STO-3G", "6-31G"),
    ("multiplicity", None, 1, None),
    ("multiplicity", True, 1, None),
    ("multiplicity", 1.0, 1, None),
    ("charge", False, 0, None),
    ("charge", 0.0, 0, None),
])
def test_scoped_condition_difference_uses_scientific_resolver_without_rewriting_evidence(
        field, value, general, effective):
    request, run = objects(scientific=False)
    request.systems = [SystemInput(id="A"), SystemInput(id="B", conditions={field: value})]
    # A historical observation remains historical even when B now requests a
    # different basis or has unknown electron state. This is an offline fixture.
    result = Result(run_id=run.id, operation_status="completed", observations={
        "historical_conditions": {"basis": "STO-3G", "charge": 0, "multiplicity": 1}})
    run.result_ids = [result.id]
    before = request.model_dump_json(), run.model_dump_json(), result.model_dump_json()
    prepared = build_context(request, run, results=[result])
    authority = payload(prepared)["AUTHORITY"]
    assert authority["request"][field] == general
    projected = authority["system_condition_overrides"]
    assert "Current Request only" in projected["meaning"]
    assert "historical qualification" in projected["meaning"]
    assert projected["rows"] == [{"system_id": "B", "conditions": {
        field: {"general": general, "effective": effective, "source": "system:B"}}}]
    resolved = effective_conditions(request, system_id="B")
    assert resolved["conditions"][field] == effective
    assert resolved["sources"][field] == "system:B"
    if effective is None:
        assert "unknown_condition:" + field in resolved["reasons"]
        prompt = prepared.body()["messages"][0]["content"]
        assert "If unknown conditions block the goal, clarify" in prompt
        assert "source evidence or execution permission cannot supply unknown user conditions" in prompt
    assert (request.model_dump_json(), run.model_dump_json(), result.model_dump_json()) == before


def test_unconfirmed_system_origin_is_unknown_and_canonical_equivalence_is_not_a_difference():
    request, run = objects(scientific=False)
    request.systems = [
        SystemInput(id="A", conditions={"method": "RHF", "environment": "gas"}),
        SystemInput(id="B", conditions={"multiplicity": 1}, conditions_source={"multiplicity": "inferred"}),
    ]
    rows = payload(build_context(request, run))["AUTHORITY"]["system_condition_overrides"]["rows"]
    assert rows == [{"system_id": "B", "conditions": {
        "multiplicity": {"general": 1, "effective": None, "source": "system:B"}}}]
    assert effective_conditions(request, system_id="B")["conditions"]["multiplicity"] is None


@pytest.mark.parametrize("port", ["raw_fields", "content_index"])
def test_completed_observation_goal_does_not_request_irrelevant_unknown_condition(port):
    request, run = objects(scientific=False)
    request.conditions["explain_results"] = True
    request.systems = [SystemInput(id="B", conditions={"multiplicity": None})]
    request.goals = [Goal(id="read", port=port, minimum_check_version="evidence-read-1")]
    result = Result(run_id=run.id, operation_status="completed", observations={port: {
        "units": None, "conditions": "unknown", "scientific_status": "unverified"}})
    run.result_ids = [result.id]
    run.goal_status = {"read": "satisfied"}
    before = request.model_dump_json(), run.model_dump_json(), result.model_dump_json()
    prepared = build_context(request, run, results=[result])
    data = payload(prepared)
    assert list(data["ACTION_PARAMETERS"]) == ["stop"]
    assert data["PROPOSAL_SCHEMA"]["properties"]["action"]["enum"] == ["stop"]
    assert "clarify" not in prepared.body()["messages"][0]["content"].lower()
    row = data["AUTHORITY"]["system_condition_overrides"]["rows"][0]
    assert row["conditions"]["multiplicity"]["effective"] is None
    assert data["DATA"]["results"][0]["unqualified_observations"][port]["units"] is None
    assert (request.model_dump_json(), run.model_dump_json(), result.model_dump_json()) == before


def test_semantic_intake_retains_user_scoped_values_without_premature_effective_projection():
    request, run = objects(scientific=False)
    request.normalization_status = "pending"
    request.systems = [SystemInput(id="B", conditions={"basis": "6-31G", "multiplicity": None})]
    before = request.model_dump_json(), run.model_dump_json()
    parameters = action_parameters(request=request)
    before_parameters = json.dumps(parameters, sort_keys=True)
    policy = parameters["normalize_request"]["questions_policy"]
    prepared = build_context(request, run, action_parameters=parameters)
    data = payload(prepared)
    assert data["AUTHORITY"]["request"]["systems"][0]["conditions"] == {
        "basis": "6-31G", "multiplicity": None}
    assert "system_condition_overrides" not in data["AUTHORITY"]
    assert list(data["ACTION_PARAMETERS"]) == ["normalize_request"]
    assert prepared.body()["messages"][0]["content"].count(policy) == 1
    assert "questions_policy" not in data["ACTION_PARAMETERS"]["normalize_request"]
    assert json.dumps(parameters, sort_keys=True) == before_parameters
    assert (request.model_dump_json(), run.model_dump_json()) == before


@pytest.mark.parametrize("case", ["sampling_left", "sampling_right", "sampling_stop"])
def test_new_joint_sampling_context_does_not_repeat_five_equal_system_conditions(tmp_path, monkeypatch, case):
    from tests.helpers import phase_b_joint

    store = Store(tmp_path / "data", environment_root=tmp_path / "environment")
    # Only fixture registration: bypass environment doctor, never execution.
    monkeypatch.setattr(phase_b_joint, "initialize_agent", lambda store, config, request, permission, budget, **kw:
                        store.create_run(request, None, permission, budget))
    run, _ = phase_b_joint.prepare_case(store, Config(), case, "development")
    request = store.load_request(run)
    assert len(request.systems) == 5
    before = request.model_dump_json(), run.model_dump_json()
    prepared = build_context(request, run, relevant_tools=run.permission.allowed_tools)
    data = payload(prepared)
    assert "system_condition_overrides" not in data["AUTHORITY"]
    assert data["AUTHORITY"]["request"]["goals"][0]["conditions"] == request.goals[0].conditions
    contract = next(tool for tool in data["TOOL_CATALOG"] if tool["name"] == "analysis.finite_sampling")["check_contract"]
    assert contract == get_tool("analysis.finite_sampling").check_contract
    assert "New samples may change minimum and neighbors" in contract["rule"]
    assert "E-Emin>energy_threshold_eh" in contract["rule"]
    assert "span<=target_width_angstrom+distance_tolerance_angstrom" in contract["rule"]
    assert prepared.input_token_bound <= 12000
    assert (request.model_dump_json(), run.model_dump_json()) == before


@pytest.mark.parametrize("final_only", [False, True])
def test_unstated_observation_units_stay_unknown_in_execution_and_final_context(final_only):
    request, run = objects(scientific=False)
    request.conditions["explain_results"] = True
    run.permission.allowed_tools = ["evidence.import", "evidence.search"]
    run.permission.source_ids = ["registered_stdout"]
    run.permission.artifact_writes = True
    if final_only:
        run.goal_status = {request.goals[0].id: "satisfied"}
    observation = {"units": None, "conditions": "unknown", "stage": "unknown", "scientific_status": "unverified",
                   "hits": [{"line": 1, "text": "FINAL SINGLE POINT ENERGY       -74.962991615317"}]}
    result = Result(run_id=run.id, operation_status="completed", observations={"search_hits": observation})
    run.result_ids = [result.id]
    before = result.model_dump_json()
    prepared = build_context(request, run, results=[result], relevant_tools=run.permission.allowed_tools)
    data = payload(prepared)
    visible = data["DATA"]["results"][0]["unqualified_observations"]["search_hits"]
    assert visible["units"] is None
    assert visible["scientific_status"] == "unverified"
    assert visible["hits"] == observation["hits"]
    prompt = prepared.body()["messages"][0]["content"].lower()
    assert "null units=unknown" in prompt and "never inferred" in prompt
    if final_only:
        assert list(data["ACTION_PARAMETERS"]) == ["stop"]
    else:
        assert "step/params/effects" in prompt
        assert "import_artifact (registers evidence)" in prompt
        imported = next(tool for tool in data["TOOL_CATALOG"] if tool["name"] == "evidence.import")
        assert imported["effects"] == ["import_artifact"]
        assert "read_registered_artifact" not in imported["effects"]
    assert result.model_dump_json() == before


_REAL_ROOT = Path(__file__).resolve().parents[2] / "data/phase-b/reference"
_REPLAYS = [
    ("array_pending", "run_48b16a03263e4542b843705122ab7004", 1),
    ("array_final", "run_48b16a03263e4542b843705122ab7004", -1),
    ("import_initial", "run_5f48def585ff48faa87f603e74b47677", 0),
    ("import_final", "run_5f48def585ff48faa87f603e74b47677", -1),
    ("comparison_basis", "run_aafbd031c9194d24a77499f5bd7f9469", -1),
    ("comparison_unknown", "run_f898335c642b4884bfe1b9fe716d3930", -1),
    ("v14_array_initial", "run_47e3358053464d4b889e7cc93cd5edc9", 0),
    ("v14_array_first_rejection", "run_47e3358053464d4b889e7cc93cd5edc9", 1),
    ("v14_array_second_rejection", "run_47e3358053464d4b889e7cc93cd5edc9", 2),
    ("v15_unknown_initial", "run_5b083256fc98409b890b2cd809243970", 0),
    ("v15_unknown_clarify_rejected", "run_5b083256fc98409b890b2cd809243970", 1),
    ("v15_unknown_clarify_accepted", "run_5b083256fc98409b890b2cd809243970", 2),
]


@pytest.mark.parametrize("case,run_id,index", _REPLAYS, ids=[item[0] for item in _REPLAYS])
@pytest.mark.parametrize("model_profile", ["disabled", "thinking_low"])
def test_retained_v8_request_rebuild_preserves_facts_without_rewriting_real_evidence(
        case, run_id, index, model_profile):
    """Actual failed development inputs; no HTTP, ORCA, regrade or outcome claim.

    This optional local replay complements the portable fixtures above. A clean
    checkout lacks ignored live records and must report those replays skipped.
    """
    directory = _REAL_ROOT / "runs" / run_id
    if not (directory / "run.json").is_file():
        pytest.skip("retained v8 development record absent; real-request replay unverified")
    before_files = {path: sha256_file(path) for path in directory.rglob("*.json")}
    store = Store(_REAL_ROOT)
    persisted = store.load_run(run_id)
    record = persisted.model_records[index]
    expected_prompt = {"v14": "agent-json-v14", "v15": "agent-json-v15"}.get(case.split("_")[0], "agent-json-v8")
    assert record["prompt_version"] == expected_prompt
    original_body = json.loads((directory / "model" / (record["id"] + ".request.json")).read_text(encoding="utf-8"))
    original = payload(SimpleNamespace(body=lambda: original_body))
    authority = original["AUTHORITY"]
    run = persisted.model_copy(deep=True)
    request = store.load_request_revision(run, authority["basis"]["request_version"])
    plan = store.load_plan_revision(run, authority["basis"]["plan_version"]) if authority["basis"]["plan_version"] else None
    run.request_version, run.plan_id, run.plan_version = request.version, plan.id if plan else None, plan.version if plan else None
    run.goal_status = authority["goal_status"]
    results = [store.load_result(run.id, result["result_id"]) for result in original["DATA"]["results"]]
    run.result_ids = [result.id for result in results]
    # The final persisted Run has already consumed this request's feedback.
    # Reconstruct the pre-send state from its archived related-result boundary,
    # so a new result cannot be mistaken for an older consumed read preview.
    run.processed_feedback = [identifier for identifier in run.result_ids
                              if identifier not in authority["related_results"]]
    run.calls = [call for call in run.calls if call.result_id in run.result_ids]
    run.selected_results = {key: value for key, value in run.selected_results.items() if value in run.result_ids}
    for field in ("model_calls", "model_tokens_used", "model_tokens_unknown", "evidence_reads", "analysis_executions"):
        setattr(run.usage, field, authority["cumulative_usage"].get(field, 0))
    before_objects = request.model_dump_json(), run.model_dump_json(), [result.model_dump_json() for result in results]
    prepared = build_context(request, run, plan, results=results,
        relevant_tools=run.permission.allowed_tools,
        feedback={**original["DATA"]["feedback"], **original["CONTROL"],
                  "new_result_ids": authority["related_results"],
                  "current_goal_use": original["DATA"].get("current_goal_use", [])},
        now=run.created_at + timedelta(seconds=30), model_profile=model_profile)
    rebuilt = payload(prepared)
    assert prepared.input_token_bound <= 12000
    assert rebuilt["AUTHORITY"]["related_results"] == authority["related_results"]
    for field in ("permission", "budget_limits", "request"):
        assert_historical_projection(rebuilt["AUTHORITY"][field], authority[field], field)
    assert rebuilt.get("CONTROL", {}) == original["CONTROL"]
    assert rebuilt["DATA"].get("current_goal_use") == original["DATA"].get("current_goal_use")
    if case.startswith("v15_"):
        raw_wire = json.loads(prepared.body()["messages"][1]["content"])
        if "ACTION_PARAMETERS" in raw_wire:
            native = raw_wire["ACTION_PARAMETERS"]["clarify"]
            assert set(native) == {"questions", "unresolved"}
            assert all(isinstance(text, str) and text == "<1-5 texts,1-1000 chars>"
                       for texts in native.values() for text in texts)
        else:
            # Terminal compact contexts omit duplicate examples, retaining the
            # exact native parameter contract in the exhaustive action schema.
            branch = next(item["properties"] for item in rebuilt["PROPOSAL_SCHEMA"]["oneOf"]
                          if item["properties"]["action"] == {"const": "clarify"})
            assert set(branch["parameters"]["required"]) == {"questions", "unresolved"}
            assert branch["parameters"]["maxProperties"] == 2
            assert branch["parameters"]["additionalProperties"] == {
                "type": "array", "minItems": 1, "maxItems": 5,
                "items": {"type": "string", "minLength": 1, "maxLength": 1000}}
        assert_historical_projection(rebuilt["AUTHORITY"]["request"], authority["request"], "request")
        assert rebuilt["AUTHORITY"]["system_condition_overrides"] == authority["system_condition_overrides"]
        for old_result, new_result in zip(original["DATA"]["results"], rebuilt["DATA"]["results"], strict=True):
            assert new_result.get("unqualified_observations") == old_result.get("unqualified_observations")
            assert new_result.get("qualified_outputs") == old_result.get("qualified_outputs")
        if index in (1, 2):
            original_wire = json.loads(original_body["messages"][1]["content"])
            assert original_wire["ACTION_PARAMETERS"]["clarify"] == {
                "questions": [{"@": 1}], "unresolved": [{"@": 1}]}
    if case.startswith("v14_"):
        assert call_tool_instruction() in prepared.body()["messages"][0]["content"]
        # The original rejected inputs already received the structured forms.
        # The new ordinary-language reminder does not establish model success.
        assert original["PROPOSAL_SCHEMA"]["then"]["properties"]["parameters"] == call_tool_parameters_schema()
        readers = [tool["name"] for tool in rebuilt["TOOL_CATALOG"]
                   if tool["effects"] == ["read_registered_artifact"]]
        assert readers == run.permission.allowed_tools == ["evidence.discover", "evidence.value"]
        # Current schemas narrow choices to the exact permitted readers and
        # coordinator-provided ready IDs; archived schema bytes stay unchanged.
        assert rebuilt["PROPOSAL_SCHEMA"]["then"]["properties"]["parameters"] == call_tool_parameters_schema(
            tool_names=readers, step_ids=rebuilt.get("CONTROL", {}).get("pending_step_ids"))
        if ready := rebuilt["CONTROL"].get("pending_step_ids"):
            assert rebuilt["ACTION_PARAMETERS"]["call_tool"] == {"step_id": ready[0]}
            for step_id in ready:
                old = next(step for step in authority["plan"]["steps"] if step["id"] == step_id)
                new = next(step for step in rebuilt["AUTHORITY"]["plan"]["steps"] if step["id"] == step_id)
                assert new["parameters"] == old["parameters"]
    for old, new in zip(original["DATA"]["results"], rebuilt["DATA"]["results"], strict=True):
        assert new["result_id"] == old["result_id"]
        for key in ("source_record_sha256", "source_sha256"):
            # Smaller prompts may fit the ordinary nested source representation
            # instead of the lossless compact form used by the retained request.
            nested = "sha256" if key == "source_sha256" else key
            assert new.get(key, new.get("source", {}).get(nested)) == old.get(key, old.get("source", {}).get(nested))
        for name, observed in old.get("unqualified_observations", {}).items():
            if name in {"content_index", "value_observation", "search_hits"}:
                assert new["unqualified_observations"][name]["units"] is None
                assert new["unqualified_observations"][name]["scientific_status"] == "unverified"
                for field in ("path", "sha256", "coverage", "hits"):
                    if field in observed:
                        assert new["unqualified_observations"][name][field] == observed[field]
    if case == "array_pending":
        ready = rebuilt["CONTROL"]["pending_step_ids"]
        assert rebuilt["ACTION_PARAMETERS"]["call_tool"] == {"step_id": ready[0]}
        for step_id in ready:
            old = next(step for step in authority["plan"]["steps"] if step["id"] == step_id)
            new = next(step for step in rebuilt["AUTHORITY"]["plan"]["steps"] if step["id"] == step_id)
            assert new["parameters"] == old["parameters"]
        assert "slice" in json.dumps(next(step for step in plan.steps if step.id == ready[0]).parameters.model_dump())
    elif case == "import_initial":
        imported = next(tool for tool in rebuilt["TOOL_CATALOG"] if tool["name"] == "evidence.import")
        assert imported["effects"] == ["import_artifact"]
        assert "import_artifact (registers evidence)" in prepared.body()["messages"][0]["content"]
    elif case.startswith("comparison_"):
        field, general, effective = (("basis", "STO-3G", "6-31G") if case == "comparison_basis"
                                     else ("multiplicity", 1, None))
        assert rebuilt["AUTHORITY"]["system_condition_overrides"]["rows"] == [
            {"system_id": "B", "conditions": {field: {
                "general": general, "effective": effective, "source": "system:B"}}}]
        # Historical science remains qualified at its original conditions; the
        # existing applicability analysis must still expose B's current mismatch.
        analysis = rebuilt["DATA"]["results"][0]["unqualified_observations"]["analysis"]
        member = next(member for member in analysis["members"] if member["member_id"] == "B")
        assert member["source"]["conditions"][field] == general
        assert member["source"]["expected_conditions"][field] == effective
        assert "source_condition_mismatch:" + field in member["source"]["mismatched_fields"]
    assert (request.model_dump_json(), run.model_dump_json(), [result.model_dump_json() for result in results]) == before_objects
    assert {path: sha256_file(path) for path in before_files} == before_files
