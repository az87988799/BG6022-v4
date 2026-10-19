"""Context for authorized task files; no model, network or process launch."""

import copy
import hashlib
import json
from pathlib import Path

import pytest

from orca_agent import context
from orca_agent.config import load_config
from orca_agent.decision_purpose import DECISION_CONTRACT_PROMPT_VERSIONS
from orca_agent.delivery import collect_delivery_snapshot
from orca_agent.models import Goal, Observation, ObservationSource, Request, Result, SystemInput
from orca_agent.orca.native_profile import profile_contract
from orca_agent.semantic import selection_action_parameters
from orca_agent.store import Store
from tests.unit.test_context import objects
from tests.unit.test_decision_purpose_capacity import decoded
from tests.unit.test_python_repair_budget import case, settle


@pytest.mark.parametrize("input_tokens", [12000, 64000])
def test_settled_failed_python_retains_repair_schema_and_frozen_code_identity(tmp_path, monkeypatch, input_tokens):
    store, original, _ = case(tmp_path, monkeypatch)
    request, plan = store.load_request(original), store.load_plan(original)
    run = store.create_run(request, plan, original.permission.model_copy(update={"model_execution": True}),
        original.budget.model_copy(update={"model_calls": 4, "model_tokens": 48000,
            "input_tokens": input_tokens, "output_tokens": 2000, "decision_rounds": 12}),
        configuration=original.configuration.model_copy(update={"model":
            original.configuration.model.model_copy(update={"input_tokens": input_tokens})}))
    attempt, result = settle(store, run)
    before = copy.deepcopy((request, run, plan, result))
    snapshot = collect_delivery_snapshot(store, run, request, plan, [result])
    if input_tokens == 12000:
        with pytest.raises(context.ContextLimitError, match="bound"):
            context.build_context(request, run, plan, results=[result], delivery_snapshot=snapshot,
                feedback={"new_result_ids": [result.id]})
        assert (request, run, plan, result) == before
        assert not run.model_records and run.usage.model_calls == 0
        return
    prepared = context.build_context(request, run, plan, results=[result], delivery_snapshot=snapshot,
        feedback={"new_result_ids": [result.id]})
    wire = decoded(prepared)
    assert "revise_plan" in wire["AUTHORITY"]["decision_purpose"]["allowed_actions"]
    assert "retaining logical_id and every non-code field" in wire["TASK_CODE_RULES"]
    assert "repair_of:failed frozen_step.id" in wire["TASK_CODE_RULES"]
    assert "do not add the replacement writer as a dependency" in wire["TASK_CODE_RULES"]
    assert not context._frozen_completed(plan, run, [result])
    if "TOOLS" in wire:
        tool, = [tool for tool in wire["TOOLS"] if tool["name"] == "python.execute"]
        schema = tool["parameters"]
    else:
        tool, = [tool for tool in wire["TOOL_CATALOG"] if tool["name"] == "python.execute"]
        schema = wire["PARAMETER_SCHEMAS"][tool["parameter_schema"]]
    assert set(schema["properties"]) == {"code", "inputs", "observation_names", "conditions"}
    assert wire["AUTHORITY"]["plan"]["steps"][0]["parameters"]["code"] == attempt.frozen_step.parameters.code.model_dump()
    assert (request, run, plan, result) == before


@pytest.mark.parametrize("authorized", [False, True])
def test_python_catalog_requires_execution_permission(authorized):
    _, run = objects()
    run.permission.allowed_tools = ["python.execute"]
    run.permission.artifact_writes = True
    run.permission.code_execution = authorized
    catalog, _ = context._tools(run, ())
    assert [tool["name"] for tool in catalog] == (["python.execute"] if authorized else [])


def test_written_code_feedback_retains_reference_hash_and_size_without_content():
    artifact = {"artifact_id": "artifact_script", "sha256": "a" * 64, "name": "script_" + "a" * 120 + ".py", "bytes": 65536,
                "role": "python", "artifact_ids": ["artifact_script"]}
    result = Result(run_id="run", operation_status="completed", source={"tool": "workspace.write"},
        artifact_ids=[artifact["artifact_id"]], observations={"written_artifact": artifact})
    before = result.model_copy(deep=True)
    view = context._result(result, 1)
    assert view["unqualified_observations"]["written_artifact"] == artifact
    assert "content" not in str(view) and result == before


def test_generic_observation_feedback_preserves_values_and_unverified_sources_at_small_preview_budget():
    record = Observation(name="vibrational_frequencies", value=[-12.5, 0.0, 1000.25], unit="cm^-1",
        sources=[ObservationSource(artifact_id="raw_hessian", sha256="b" * 64,
            location="$vibrational_frequencies", attempt_id="attempt", internal_request_key="calculation")],
        code_sha256="c" * 64, actual_conditions={"method": "PBE", "origin": "admitted_native_input"},
        validation_status="unverified", limitations=["No independent Hessian check."])
    result = Result(run_id="run", operation_status="completed", observation_records=[record])
    before = result.model_copy(deep=True)
    view = context._result(result, 1)
    assert view["unqualified_observation_records"] == [record.model_dump(mode="json")]
    assert "qualified_outputs" not in view and result == before


def test_actual_live028_disclosure_gaps_remain_visible_without_rewriting_results():
    path = Path(__file__).parents[1] / "fixtures/code_contracts/live028-observation-disclosure-gap.json"
    assert hashlib.sha256(path.read_bytes()).hexdigest() == (
        "34d36cc70d3a9f67d5eff4471ed01d1163e8e33fee5bed34d2e0e2a79b8cd378")
    data = json.loads(path.read_text(encoding="utf-8"))
    result = Result.model_validate(data["result"])
    before = result.model_copy(deep=True)
    view = context._result(result, 1)
    records = {record["name"]: record for record in view["unqualified_observation_records"]}
    geometry, frequencies = records["optimized_geometry"], records["vibrational_frequencies"]
    assert isinstance(geometry["value"], str) and geometry["unit"] is None
    assert len(data["raw_frequency_values"]) == 27
    assert len(frequencies["value"]) == 21
    assert frequencies["value"] == [value for value in data["raw_frequency_values"] if value > 1e-6]
    assert not any("filter" in text.casefold() for text in frequencies["limitations"])
    assert all(record["validation_status"] == "unverified" for record in records.values())
    assert view["unqualified_observation_records"] == data["result"]["observation_records"]
    assert "qualified_outputs" not in view and result == before
    # Prompt guidance does not retrospectively restore omitted modes, infer a
    # unit, add a limitation to historical evidence or create a scientific pass.


@pytest.mark.parametrize("input_tokens", [12000, 64000])
def test_settled_writer_receipt_remains_after_feedback_without_inlining_task_source(tmp_path, monkeypatch, input_tokens):
    from tests.unit.test_generated_delivery import case as writer_case
    from tests.unit.test_generated_delivery import written
    store, original, request, writer = writer_case(tmp_path, monkeypatch)
    plan = store.load_plan(original)
    run = store.create_run(request, plan, original.permission.model_copy(update={"model_execution": True}),
        original.budget.model_copy(update={"model_calls": 4, "model_tokens": 48000,
            "input_tokens": input_tokens, "output_tokens": 2000, "decision_rounds": 12}),
        configuration=original.configuration.model_copy(update={"model":
            original.configuration.model.model_copy(update={"input_tokens": input_tokens})}))
    result, reference = written(store, run, writer)
    run.processed_feedback.append(result.id)
    snapshot = collect_delivery_snapshot(store, run, request, plan, [result])
    before = copy.deepcopy((request, run, plan, result, snapshot))
    if input_tokens == 12000:
        with pytest.raises(context.ContextLimitError, match="bound"):
            context.build_context(request, run, plan, results=[result], delivery_snapshot=snapshot)
        assert (request, run, plan, result, snapshot) == before
        assert not run.model_records and run.usage.model_calls == 0
        return
    prepared = context.build_context(request, run, plan, results=[result], delivery_snapshot=snapshot)
    assert prepared.input_token_bound <= input_tokens
    wire = decoded(prepared)
    receipt = next(item for item in wire["DATA"]["results"] if item["result_id"] == result.id)
    value = receipt["unqualified_observations"]["written_artifact"]
    assert {key: value[key] for key in reference} == reference
    assert value["bytes"] == len(writer.parameters.content.encode()) and value["role"] == "python"
    assert "content" not in value and (request, run, plan, result, snapshot) == before
    def strings(item):
        if isinstance(item, dict):
            return [text for child in item.values() for text in strings(child)]
        if isinstance(item, list):
            return [text for child in item for text in strings(child)]
        return [item] if isinstance(item, str) else []
    assert writer.parameters.content not in strings(wire)


@pytest.mark.parametrize("input_tokens", [12000, 64000])
def test_actual_collected_observation_returns_to_same_loop_with_unverified_value(tmp_path, monkeypatch, input_tokens):
    from tests.unit.test_generated_delivery import case as writer_case
    from tests.unit.test_generated_delivery import execute_fixture, written
    store, original, request, writer = writer_case(tmp_path, monkeypatch)
    run = store.create_run(request, store.load_plan(original), original.permission.model_copy(update={"model_execution": True}),
        original.budget.model_copy(update={"model_calls": 4, "model_tokens": 48000,
            "input_tokens": input_tokens, "output_tokens": 2000, "decision_rounds": 12}),
        configuration=original.configuration.model_copy(update={"model":
            original.configuration.model.model_copy(update={"input_tokens": input_tokens})}))
    receipt, reference = written(store, run, writer)
    run, _, result = execute_fixture(store, run, reference)
    plan = store.load_plan(run)
    snapshot = collect_delivery_snapshot(store, run, request, plan, [receipt, result])
    before = copy.deepcopy((request, run, plan, receipt, result, snapshot))
    if input_tokens == 12000:
        with pytest.raises(context.ContextLimitError, match="bound"):
            context.build_context(request, run, plan, results=[receipt, result], delivery_snapshot=snapshot,
                feedback={"new_result_ids": [result.id]})
        assert (request, run, plan, receipt, result, snapshot) == before
        assert not run.model_records and run.usage.model_calls == 0
        return
    prepared = context.build_context(request, run, plan, results=[receipt, result], delivery_snapshot=snapshot,
        feedback={"new_result_ids": [result.id]})
    wire = decoded(prepared)
    prompt = prepared.body()["messages"][0]["content"]
    assert prepared.input_token_bound <= input_tokens
    assert "ACTION_SCHEMAS" in wire and "PROPOSAL_SCHEMA" not in wire
    assert "No fabricated evidence or unauthorized code/paths." in prompt
    assert "No fabricated evidence/code/paths" not in prompt
    assert run.permission.scientific_execution is False
    assert "filename:task-relative path" in wire["TASK_CODE_RULES"]
    assert "internal_request_key" not in wire["TASK_CODE_RULES"]
    assert "No shell/subprocess/network/host paths" in wire["TASK_CODE_RULES"]
    assert "Observations unqualified" in wire["TASK_CODE_RULES"]
    feedback = next(item for item in wire["DATA"]["results"] if item["result_id"] == result.id)
    assert feedback["unqualified_observation_records"] == [item.model_dump(mode="json") for item in result.observation_records]
    observed, = feedback["unqualified_observation_records"]
    assert observed["value"] == "observed text" and observed["validation_status"] == "unverified"
    assert observed["sources"] and observed["limitations"] and "qualified_outputs" not in feedback


def selection_context(tmp_path, *, source=None, authorized=True, systems=(), native_only=False):
    config = load_config(Path("config.code.example.toml"))
    original = "Compute the requested property; choose a suitable method and basis from documentation."
    request = Request(original_text=original, method=None, basis=None,
        systems=list(systems),
        goals=[Goal(id="frequencies", port="vibrational_frequencies", quantity="vibrational frequencies",
            minimum_check_version="generic-observation-1", evidence_contract_version="generic-observation-1",
            verification="reported_with_limitations")],
        conditions={"electronic_state": "RKS", "environment": "gas_phase"},
        conditions_source={"charge": "explicit", "multiplicity": "explicit",
            "electronic_state": "default", "environment": "default",
            **({"method": source} if source else {})},
        automatic_selection=["method", "basis"] if authorized else [],
        messages=[{"id": "user_1", "text": original}],
        condition_evidence={"automatic_selection." + field: {
            "text_basis": "choose a suitable method and basis from documentation", "message_id": "user_1"}
            for field in ("method", "basis")} if authorized else {})
    store = Store(tmp_path / "data", environment_root=tmp_path / "environment")
    permission = config.text.permission
    if native_only:
        permission = permission.model_copy(update={"code_execution": False,
            "allowed_tools": [name for name in permission.allowed_tools if name != "python.execute"]})
    run = store.create_run(request, None, permission, config.text.budget,
                           configuration=config.run_configuration())
    snapshot = collect_delivery_snapshot(store, run, request)
    prepared = context.build_context(request, run, delivery_snapshot=snapshot)
    return request, run, prepared, decoded(prepared)


def test_authorized_selection_exposes_sources_code_tools_and_exact_frozen_native_contract(tmp_path):
    request, run, prepared, wire = selection_context(tmp_path)
    assert prepared.input_token_bound <= run.budget.input_tokens
    assert "select_conditions" in wire["AUTHORITY"]["decision_purpose"]["allowed_actions"]
    selection = wire["ACTION_SCHEMAS"]["select_conditions"]
    from orca_agent.readable_context import native_schema
    assert selection == native_schema(selection_action_parameters(request))
    assert selection["properties"]["request_version"]["const"] == request.version
    assert selection["properties"]["choices"]["propertyNames"]["enum"] == ["method", "basis"]
    tools = {tool["name"]: tool for tool in wire["TOOLS"]}
    assert {"python.execute", "orca.run", "workspace.write", "knowledge.search",
            "structure.resolve", "structure.prepare", "evidence.text", "evidence.search"} <= tools.keys()
    assert set(tools["python.execute"]["parameters"]["properties"]) == {
        "code", "inputs", "observation_names", "conditions"}
    assert set(tools["workspace.write"]["parameters"]["properties"]) == {"name", "content", "role"}
    assert wire["AUTHORITY"]["native_input_contract"] == profile_contract(run.configuration.execution.native_input_policy)
    assert wire["AUTHORITY"]["native_input_contract"]["scientific_validation"] == "unverified"
    native = wire["AUTHORITY"]["native_input_contract"]
    raw_content = prepared.body()["messages"][1]["content"]
    for block in ("scf", "geom", "method"):
        # Actual wire strings remain self-contained even if surrounding keys
        # are compacted: a decoder cannot hide a missing native block name.
        assert f"%{block} " in native["required_blocks"][block]
        assert native["required_blocks"][block] in raw_content
    assert "1..128" in native["required_blocks"]["method"]
    assert "Every field needs a value" in native["block_syntax"]
    assert native["optional_blocks"]["output"]["example"] == "%output JsonPropFile true end"
    assert native["optional_blocks"]["output"]["fields"] == {"jsonpropfile": ["true", "false", "0", "1"]}
    assert native["optional_blocks"]["output"]["required"] is False
    rules = wire["TASK_CODE_RULES"]
    assert "from orca_task_sdk import run_orca" in rules and "Step.parameters.observation_names" in rules
    assert "missing controls are rejected, never filled in" in rules
    assert "request_key unique per Run" in rules
    assert "prior receipts cannot replay" in rules and "same cumulative budgets" in rules
    assert "unknown work:reconcile first" in rules
    assert "separately authorized" in rules and "Observations unqualified" in rules
    assert "exact excerpt quote" in wire["CONDITION_SELECTION_RULES"]
    assert str(run.configuration.execution.docker_path) not in prepared.body()["messages"][1]["content"]
    assert not any(recipe in rules.lower() for recipe in ("ethanol", "cco", "乙醇"))


def test_observation_guidance_requires_full_sequences_and_sourced_units_without_task_algorithm(tmp_path):
    request, run, prepared, wire = selection_context(tmp_path)
    rules = wire["TASK_CODE_RULES"]
    assert "full parsed sequences, including negative/near-zero values" in rules
    assert "Additional filtered subsets must label filtering/omissions" in rules
    assert "cannot replace full data or prove absence of anomalies" in rules
    assert "sourced physical units even for strings/coordinate files" in rules
    assert "Unknown units stay null with explicit limitations, never guessed" in rules
    assert "dimensionless quantities explicitly" in rules
    assert not any(task_detail in rules for task_detail in ("value > 1.0e-6", "276.12", "cm^-1", "angstrom"))
    assert prepared.input_token_bound <= 64000
    assert Store(tmp_path / "data").load_run(run.id) == run
    assert Store(tmp_path / "data").load_request(run) == request


@pytest.mark.parametrize("native_only", [False, True])
def test_execution_resource_projection_uses_frozen_nondefault_values_without_permission_changes(tmp_path, native_only):
    request, run, _, _ = selection_context(tmp_path, native_only=native_only)
    frozen_values = {"max_cores": 3, "total_memory_mb": 1536, "maxcore_mb": 128,
                     "job_timeout_seconds": 654.0, "script_timeout_seconds": 321.0}
    run.configuration = run.configuration.model_copy(update={
        "resources": run.configuration.resources.model_copy(update=frozen_values)})
    before = run.model_dump_json()
    snapshot = collect_delivery_snapshot(Store(tmp_path / "data"), run, request)
    prepared = context.build_context(request, run, delivery_snapshot=snapshot)
    wire = decoded(prepared)
    assert wire["AUTHORITY"]["execution_resources"] == frozen_values
    assert run.model_dump_json() == before
    assert wire["AUTHORITY"]["permission"]["code_execution"] is (not native_only)
    assert wire["AUTHORITY"]["permission"]["allowed_tools"] == run.permission.allowed_tools
    assert "environment_concurrency" not in wire["AUTHORITY"]["execution_resources"]
    if not native_only:
        assert "AUTHORITY.execution_resources.maxcore_mb" in wire["TASK_CODE_RULES"]
    assert prepared.input_token_bound <= run.budget.input_tokens


def test_sdk_guidance_exposes_readonly_filename_mapping_and_repair_constraints(tmp_path):
    _, _, _, wire = selection_context(tmp_path)
    rules = wire["TASK_CODE_RULES"]
    assert "Return dict keys:request_key,input_fingerprint,operation_status,scientific_validation,actual_conditions,files" in rules
    assert "files maps original filenames to readable read-only copy paths, not Artifact metadata" in rules
    assert "includes inputs/dependencies" in rules
    assert "Suffix or mapping order cannot identify outputs" in rules
    assert "Input=job.inp; OptFreq final geometry=job.xyz; Hessian=job.hess" in rules
    assert "Inspect files.keys()" in rules
    assert "stdout.out/stderr.txt" in rules and "check existence before reading" in rules
    assert "Never infer output names from input_path" in rules
    assert "Declared relative dependencies" in rules
    assert "Cite outputs, not copied inputs" in rules
    assert "filename=original files key and internal_request_key=receipt.request_key" in rules
    assert "never the copy path" in rules


def test_native_only_permission_keeps_authoring_and_bounded_read_schemas_without_python(tmp_path):
    _, run, prepared, wire = selection_context(tmp_path, native_only=True)
    tools = {tool["name"]: tool for tool in wire["TOOLS"]}
    assert "python.execute" not in tools and "TASK_CODE_RULES" not in wire
    assert run.permission.code_execution is False
    assert wire["AUTHORITY"]["permission"]["code_execution"] is False
    assert {"orca.run", "workspace.write", "knowledge.search", "evidence.text", "evidence.search"} <= tools.keys()
    assert set(tools["orca.run"]["parameters"]["properties"]) == {"input", "inputs", "observation_names", "conditions"}
    assert set(tools["workspace.write"]["parameters"]["properties"]) == {"name", "content", "role"}
    for name in ("knowledge.search", "evidence.text", "evidence.search"):
        assert isinstance(wire["ACTION_SCHEMAS"][name], dict)
        assert wire["ACTION_SCHEMAS"][name]["properties"]
    assert wire["AUTHORITY"]["native_input_contract"] == profile_contract(run.configuration.execution.native_input_policy)
    assert prepared.input_token_bound <= run.budget.input_tokens


def test_staged_code_guidance_preserves_science_goals_without_future_artifacts(tmp_path):
    from orca_agent.proposals import ProposalError, materialize_plan

    request, run, _, wire = selection_context(tmp_path)
    before = copy.deepcopy((request, run))
    rules = wire["TASK_CODE_RULES"]
    assert "workspace.write/input-preparation first; consuming Steps need real artifact_id/sha256" in rules
    assert "Unmet Goals use" in rules
    assert "goal_map[Goal.id]={gap,port:Goal.port}" in rules
    assert "Execute writers, then revise_plan from file receipts" in rules
    assert "No placeholders/future producer values in parameters.code/input/inputs" in rules
    assert "CodeRef.language must match bytes, never converts" in rules
    assert "python.execute.code needs Python" in rules
    assert "orca.run.input needs an ORCA program, never XYZ" in rules
    assert "Geometry belongs in parameters.inputs=manifest" in rules
    assert "parameters.inputs=manifest" in rules
    assert "Step.inputs[role]={producer_key,port} binds evidence ports, not manifest" in rules
    store = Store(tmp_path / "data", environment_root=tmp_path / "environment")
    candidate = {"steps": [{"key": "write", "tool": "workspace.write", "parameters": {
        "name": "task.py", "content": "pass\n", "role": "python"}}],
        "goal_map": {goal.id: {"port": goal.port, "gap": "task artifacts not written yet"}
                     for goal in request.goals}}
    plan = materialize_plan(store, run, candidate)
    plan.validate_request(request)
    assert len(plan.steps) == 1 and plan.steps[0].tool == "workspace.write"
    assert all(plan.goal_map[g.id].gap and plan.goal_map[g.id].port == g.port for g in request.goals)
    # Guidance must not add a producer-reference form to CodeRef or treat the
    # Step's evidence bindings as an input-file manifest.
    future = copy.deepcopy(candidate)
    future["steps"].append({"key": "execute", "tool": "python.execute", "parameters": {
        "code": {"producer_key": "write", "port": "written_artifact"}}})
    with pytest.raises(ProposalError):
        materialize_plan(store, run, future)
    manifest_in_evidence = copy.deepcopy(candidate)
    manifest_in_evidence["steps"][0]["inputs"] = {"version": 1, "entries": []}
    with pytest.raises(ProposalError):
        materialize_plan(store, run, manifest_in_evidence)
    assert (request, run) == before
    assert store.load_plan(run) is None and not run.attempts and not run.calls


def test_scientific_sdk_guidance_declares_current_source_and_resource_limits(tmp_path):
    _, run, _, wire = selection_context(tmp_path)
    rules = wire["TASK_CODE_RULES"]
    assert "Compute+parse in one Python task" in rules
    assert "SDK native receipt must be completed in that same Attempt" in rules
    assert "orca.run only archives raw outputs" in rules
    assert "separate Python parsing imported stdout lacks scientific Goal condition provenance" in rules
    assert "ORCA %maxcore uses AUTHORITY.execution_resources.maxcore_mb per process, never total_memory_mb" in rules
    assert "pure file tasks need no ORCA" in rules
    assert run.permission.scientific_execution is True
    assert all(str(value) not in rules for value in (192, 1024))


@pytest.mark.parametrize("permission", [False, True])
def test_python_without_native_authorization_does_not_advertise_sdk_execution(tmp_path, monkeypatch, permission):
    store, original, _ = case(tmp_path, monkeypatch)
    request, plan = store.load_request(original), store.load_plan(original)
    run = store.create_run(request, plan, original.permission.model_copy(update={
        "model_execution": True, "scientific_execution": permission}),
        original.budget.model_copy(update={"model_calls": 4, "model_tokens": 48000,
            "input_tokens": 12000, "output_tokens": 2000, "decision_rounds": 12}))
    snapshot = collect_delivery_snapshot(store, run, request, plan)
    prepared = context.build_context(request, run, plan, delivery_snapshot=snapshot)
    rules = decoded(prepared)["TASK_CODE_RULES"]
    assert "full parsed sequences" in rules and "Unknown units stay null" in rules
    if permission:
        assert "Scientific Goals need a same-Attempt SDK native receipt" in rules
        assert "pure file tasks need no ORCA" in rules
    else:
        assert "File tasks need no ORCA." in rules
    assert "run_orca(" not in rules and "%maxcore" not in rules
    assert "orca.run" not in run.permission.allowed_tools
    assert prepared.input_token_bound <= 12000


@pytest.mark.parametrize("source,authorized", [("unknown", True), (None, False)])
def test_missing_conditions_do_not_become_selectable_without_usable_user_authorization(tmp_path, source, authorized):
    _, _, _, wire = selection_context(tmp_path, source=source, authorized=authorized)
    assert wire["AUTHORITY"]["decision_purpose"]["allowed_actions"] == ["clarify", "stop"]
    assert "select_conditions" not in wire["ACTION_SCHEMAS"]


def test_legacy_context_does_not_gain_task_code_or_native_policy_and_prompt_history_survives():
    request, run = objects()
    prepared = context.build_context(request, run)
    wire = decoded(prepared)
    assert prepared.prompt_version == "agent-json-v58"
    assert {"agent-json-v22", "agent-json-v34", "agent-json-v35", "agent-json-v36", "agent-json-v37", "agent-json-v38", "agent-json-v39", "agent-json-v40", "agent-json-v41", "agent-json-v42", "agent-json-v43", "agent-json-v44", "agent-json-v45", "agent-json-v46", "agent-json-v47"} <= DECISION_CONTRACT_PROMPT_VERSIONS
    assert "TASK_CODE_RULES" not in wire and "native_input_contract" not in wire["AUTHORITY"]
    assert run.permission.code_execution is False


def test_context_code_allowance_counts_historical_attempts_without_rewriting_usage(tmp_path, monkeypatch):
    store, original, _ = case(tmp_path, monkeypatch, initial_steps=2)
    request, plan = store.load_request(original), store.load_plan(original)
    run = store.create_run(request, plan, original.permission.model_copy(update={"model_execution": True}),
        original.budget.model_copy(update={"model_calls": 4, "model_tokens": 48000,
            "input_tokens": 12000, "output_tokens": 2000, "decision_rounds": 12}))
    for step in plan.steps:
        attempt = store.reserve_attempt(run, step)
        store.finish_attempt(run, attempt.id, state="failed", started=False, termination_confirmed=True)
    run.usage.code_retries = 0  # Read a historical counter alongside retained Attempt evidence.
    snapshot = collect_delivery_snapshot(store, run, request, plan)
    wire = decoded(context.build_context(request, run, plan, delivery_snapshot=snapshot))
    assert wire["AUTHORITY"]["remaining"]["code_retries"] == 0
    assert wire["AUTHORITY"]["code_attempt_allowance"]["effective_used"] == 1
    assert wire["AUTHORITY"]["code_attempt_allowance"]["persisted_used"] == run.usage.code_retries == 0


def test_prior_source_receipts_remain_available_after_multiple_searches(tmp_path):
    from orca_agent.tools.dispatch import execute_call
    request, run, _, _ = selection_context(tmp_path)
    store = Store(tmp_path / "data", environment_root=tmp_path / "environment")
    first = execute_call(store, run, "knowledge.search", {"document": "guide_method", "query": "method"})
    run.processed_feedback.append(first.id)
    store.save_run(run)
    second = execute_call(store, run, "knowledge.search", {"document": "guide_input", "query": "input"})
    snapshot = collect_delivery_snapshot(store, run, request, results=[first, second])
    wire = decoded(context.build_context(request, run, results=[first, second], delivery_snapshot=snapshot,
        feedback={"new_result_ids": [second.id]}))
    sources = {item["result_id"]: item["unqualified_observations"]["document_excerpt"] for item in wire["DATA"]["results"]}
    assert sources == {item.id: item.observations["document_excerpt"] for item in (first, second)}
    assert wire["AUTHORITY"]["related_results"] == [second.id]


def test_mixed_identity_rules_are_visible_per_system_without_upgrading_legacy_contract(tmp_path):
    from orca_agent.input_bindings import input_check_version, required_input_rule
    from orca_agent.tools.registry import get_tool
    systems = [SystemInput(id="old", geometry_source="prepare", identity={"contract": "named-identity-1"}),
               SystemInput(id="new", geometry_source="prepare", identity={"contract": "general-identity-2"})]
    request, _, _, wire = selection_context(tmp_path, systems=systems)
    rules = wire["AUTHORITY"]["current_input_rules_by_system"]
    tool = get_tool("structure.prepare")
    for system in systems:
        assert rules[system.id][tool.name] == {
            "check_version": input_check_version(tool, request, system.id),
            "required_input_checks": {"resolved_identity": required_input_rule(tool, request, system.id, "resolved_identity")}}
    assert rules["old"][tool.name]["check_version"] == tool.check_version
    assert rules["new"][tool.name]["check_version"] == "structure-prepare-2"


@pytest.mark.parametrize("code_authorized", [False, True])
def test_current_generated_intake_uses_single_source_scope_and_v3_schema(tmp_path, code_authorized):
    from orca_agent import semantic
    from orca_agent.readable_context import native_schema
    request, run, _, _ = selection_context(tmp_path)
    snapshot = collect_delivery_snapshot(Store(tmp_path / "data"), run, request)
    contract = semantic.action_parameters(request=request, text_input=True,
        allowed_tools=run.permission.allowed_tools, code_authorized=code_authorized)
    prepared = context.build_context(request, run, delivery_snapshot=snapshot,
        action_parameters=contract,
        user_messages=[{"id": "new_message", "text": "Preserve the original quantity and source requirements."}])
    wire = decoded(prepared)
    schema = wire["ACTION_SCHEMAS"]["normalize_request"]
    expected = native_schema(semantic.SemanticCandidate.model_json_schema())
    assert schema["properties"]["schema_version"] == {"const": semantic.VERSION}
    for field in ("identities", "automatic_selection"):
        assert schema["properties"][field] == expected["properties"][field]
    visible = wire["INTENT_RULES"]
    if code_authorized:
        assert visible["generated_task_intent"] == contract["normalize_request"]["generated_task_intent"]
        assert "confidently known English lookup alias" in visible["generated_task_intent"]
        assert "independent confirmation" in visible["generated_task_intent"]
        assert "do not guess an unknown alias" in visible["generated_task_intent"]
        assert prepared.input_token_bound <= run.budget.input_tokens <= 64000
        assert "science_scope" not in visible
        assert visible["legacy_qualified_scope"] == contract["normalize_request"]["legacy_qualified_scope"]
        assert "unsupported_system" not in visible["notice_choices"]
        assert "port_rules" not in visible["minimum_evidence_rules"]
        assert visible["minimum_evidence_rules"]["legacy_qualified_port_rules"]
    else:
        assert "generated_task_intent" not in visible and "legacy_qualified_scope" not in visible
        assert visible["science_scope"] == contract["normalize_request"]["science_scope"]
        assert "unsupported_system" in visible["notice_choices"]
        assert visible["minimum_evidence_rules"]["port_rules"]
    assert "TASK_CODE_RULES" not in wire  # Intake registers intent; it cannot execute code.


@pytest.mark.parametrize("code_permission,tool_present", [(False, True), (True, False), (True, True)])
def test_actual_decision_passes_code_permission_and_eligible_tool_to_intake(tmp_path, monkeypatch,
                                                                        code_permission, tool_present):
    from orca_agent import agent, semantic
    request, run, _, _ = selection_context(tmp_path)
    store = Store(tmp_path / "data", environment_root=tmp_path / "environment")
    run.permission.code_execution = code_permission
    if not tool_present:
        run.permission.allowed_tools.remove("python.execute")
    run = store.create_run(request, None, run.permission, run.budget, configuration=run.configuration)
    store.enqueue_message(run.id, "Preserve the requested quantities and authorized choices.")
    original = semantic.action_parameters
    seen = []
    def capture(*args, **kwargs):
        value = original(*args, **kwargs)
        seen.append((kwargs["code_authorized"], value))
        return value
    class BeforeHTTP(Exception):
        pass
    def stop_before_http(store, run, prepared, transport, **kwargs):
        rules = decoded(prepared)["INTENT_RULES"]
        assert ("generated_task_intent" in rules) is (code_permission and tool_present)
        raise BeforeHTTP
    monkeypatch.setattr(semantic, "action_parameters", capture)
    monkeypatch.setattr(agent, "send_model", stop_before_http)
    with pytest.raises(BeforeHTTP):
        agent._decision(store, store.load_run(run.id), None, [], None, None, None)
    assert seen[0][0] is (code_permission and tool_present)
    persisted = store.load_run(run.id)
    assert not persisted.model_records and not persisted.attempts and not persisted.calls


def test_compact_intake_keeps_complete_source_schema_and_literal_authority():
    from orca_agent import semantic
    request, run = objects()
    contract = semantic.action_parameters(request=request)
    before = copy.deepcopy(contract)
    prepared = context.build_context(request, run, relevant_tools=[], action_parameters=contract)
    wire = decoded(prepared)

    def unfold(node, root):
        # Independent schema-only expansion: never decode strings/property names
        # or values inside const/enum as references or validation keywords.
        if not isinstance(node, dict):
            return node
        if set(node) == {"$ref"}:
            target = root
            for part in node["$ref"].removeprefix("#/").split("/"):
                target = target[part]
            return unfold(target, root)
        result = {}
        for key, value in node.items():
            if key == "$defs":
                continue
            if key in {"properties", "patternProperties"}:
                value = {name: unfold(child, root) for name, child in value.items()}
            elif key in {"items", "additionalProperties", "propertyNames", "if", "then"}:
                value = unfold(value, root)
            elif key in {"anyOf", "oneOf", "allOf"}:
                value = [unfold(child, root) for child in value]
            result[key] = value
        if result.get("type") == "string" and (isinstance(result.get("const"), str)
                or result.get("enum") and all(isinstance(value, str) for value in result["enum"])):
            result.pop("type")
        branches = result.get("anyOf", [])
        if (set(result) == {"anyOf"} and len(branches) == 2
                and branches[1] == {"type": "null"} and set(branches[0]) == {"enum"}):
            return {"enum": [*branches[0]["enum"], None]}
        if (set(result) == {"anyOf"} and len(branches) == 2
                and branches[1] == {"type": "null"} and branches[0].get("type") == "string"
                and set(branches[0]) <= {"type", "pattern", "minLength", "maxLength"}):
            return {**branches[0], "type": ["string", "null"]}
        return result

    original = contract["normalize_request"]["schema"]
    schema = wire["PROPOSAL_SCHEMA"]
    assert unfold(schema["properties"]["parameters"], schema) == unfold(original, original)
    permission = wire["AUTHORITY"]["permission"]
    assert permission["code_execution"] == run.permission.code_execution is False
    assert permission["allowed_tools"] == run.permission.allowed_tools
    assert permission["access_bindings"]["counts"] == {"artifact_ids": 1, "source_ids": 0, "result_ids": 0}
    assert prepared.input_token_bound <= run.budget.input_tokens == 12000
    assert contract == before
