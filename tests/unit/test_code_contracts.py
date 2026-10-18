"""General scientific records do not imply an enabled execution or checking chain."""

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from orca_agent.delivery import delivery_snapshot
from orca_agent.models import (
    CalculationParameters,
    CodeExecutionParameters,
    CodeRef,
    Goal,
    InputManifest,
    InputManifestEntry,
    InternalCall,
    NativeOrcaParameters,
    Observation,
    ObservationSource,
    OutputBinding,
    Plan,
    Request,
    Result,
    Run,
    Step,
    SystemInput,
    fingerprint,
)
from orca_agent.tools.registry import get_tool

DIGEST = "a" * 64


def entry(target="inputs/ethanol.xyz", **changes):
    return InputManifestEntry(**{
        "artifact_id": "ethanol", "sha256": DIGEST, "target_path": target,
        "purpose": "initial geometry", **changes,
    })


def code_parameters(**changes):
    return CodeExecutionParameters(**{
        "code": CodeRef(artifact_id="program", sha256=DIGEST),
        "inputs": InputManifest(entries=[entry()]),
        "observation_names": ["excitation_energy"],
        "conditions": {"method": "PBE0", "basis": "def2-TZVP", "multiplicity": 1},
        **changes,
    })


def test_general_scientific_parameters_and_selection_sources_remain_distinct():
    parameters = CalculationParameters(method="PBE0", basis="def2-TZVP", charge=-1,
                                       multiplicity=2, scf_maxiter=1000, opt_maxiter=500)
    request = Request(
        method=parameters.method, basis=parameters.basis, charge=-1, multiplicity=2,
        goals=[Goal(id="uvvis", port="excitation_energy", quantity="electronic excitation energy",
                    evidence_contract_version="generic-observation-1",
                    minimum_check_version="generic-observation-1",
                    verification="reported_with_limitations",
                    minimum_evidence=["state_identity"],
                    minimum_evidence_source={"state_identity": "explicit"})],
        conditions_source={"method": "auto_selected", "basis": "auto_selected",
                           "charge": "explicit", "multiplicity": "inherited"},
        automatic_selection=["method", "basis"],
        selection_reasons={"method": "Chosen for the requested excitation analysis"},
    )
    assert request.goals[0].port == "excitation_energy"
    assert request.goals[0].minimum_evidence_source == {"state_identity": "explicit"}
    assert request.conditions_source["method"] != request.conditions_source["charge"]
    assert Request.model_validate_json(request.model_dump_json()) == request


@pytest.mark.parametrize("field,value", [
    ("method", ""), ("basis", " "), ("method", "PBE0\n%pal nprocs 8 end"),
    ("method", "\x00"), ("basis", "def2-SVP\x01"), ("multiplicity", 0),
])
def test_general_parameter_labels_cannot_smuggle_native_input(field, value):
    with pytest.raises(ValidationError):
        CalculationParameters(**{field: value})


@pytest.mark.parametrize("target", [
    "../outside.xyz", "/outside.xyz", "C:/outside.xyz", "inputs\\outside.xyz",
    "inputs/../outside.xyz", "inputs//water.xyz", "inputs/./water.xyz",
    "CON", "input/AUX.txt", "input/file.", "input/file ", "input/a:b", "input/*.xyz",
    "input/a\x00.xyz", "input/a\n.xyz",
])
def test_manifest_rejects_host_paths_and_windows_aliases(target):
    with pytest.raises(ValidationError, match="relative task path"):
        entry(target)


def test_manifest_rejects_collisions_and_conflicting_content_identity():
    for entries in (
        [entry("inputs/A.xyz"), entry("INPUTS/a.xyz")],
        [entry("inputs"), entry("inputs/a.xyz")],
        [entry("inputs/a.xyz"), entry("inputs/b.xyz", sha256="b" * 64)],
    ):
        with pytest.raises(ValidationError):
            InputManifest(entries=entries)
    with pytest.raises(ValidationError, match="Artifact hash"):
        code_parameters(code=CodeRef(artifact_id="ethanol", sha256="b" * 64))


def test_code_step_preserves_pinned_inputs_and_dynamic_quantity_without_execution():
    parameters = code_parameters()
    step = Step(id="analyze", logical_id="analyze", tool="python.execute", parameters=parameters)
    plan = Plan(request_id="request", steps=[step], goal_map={
        "uvvis": OutputBinding(step_id="analyze", port="excitation_energy"),
    })
    loaded = Plan.model_validate_json(plan.model_dump_json())
    assert isinstance(loaded.steps[0].parameters, CodeExecutionParameters)
    assert loaded.steps[0].parameters.code.sha256 == DIGEST
    assert loaded.goal_map["uvvis"].port == "excitation_energy"
    assert fingerprint(loaded) == fingerprint(plan)
    assert get_tool("python.execute").check_contract["execution_available"] is False
    assert get_tool("python.execute").output_ports == []
    with pytest.raises(ValidationError, match="goal's port"):
        Plan(request_id="request", steps=[step], goal_map={
            "uvvis": OutputBinding(step_id="analyze", port="unrequested_quantity"),
        })


def test_general_plan_and_system_sizes_are_not_fixed_scientific_enumerations():
    request = Request(id="request", goals=[Goal(id="excitation", port="excitation_energy")],
                      systems=[SystemInput(id=f"system_{index}") for index in range(6)])
    steps = [Step(id=f"analyze_{index}", logical_id=f"analyze_{index}", tool="python.execute",
                  parameters=code_parameters()) for index in range(9)]
    plan = Plan(request_id=request.id, steps=steps, goal_map={
        "excitation": OutputBinding(step_id="analyze_8", port="excitation_energy"),
    })
    assert len(request.systems) == 6
    assert len(plan.steps) == 9
    assert "maxItems" not in Plan.model_json_schema()["properties"]["steps"]


def test_native_input_has_own_binding_and_cannot_be_used_as_python():
    native = CodeRef(artifact_id="orca_input", sha256=DIGEST, language="orca")
    parameters = NativeOrcaParameters(input=native, inputs=InputManifest(entries=[entry()]),
                                      observation_names=["frequencies"])
    step = Step(id="frequency", logical_id="frequency", tool="orca.run", parameters=parameters)
    assert step.geometry is None
    assert step.parameters.input.language == "orca"
    assert get_tool("orca.run").check_contract["execution_available"] is False
    with pytest.raises(ValidationError, match="Python code"):
        CodeExecutionParameters(code=native)
    with pytest.raises(ValidationError, match="ORCA input"):
        NativeOrcaParameters(input=CodeRef(artifact_id="python_code", sha256=DIGEST))
    with pytest.raises(ValidationError):
        CodeRef(artifact_id="code", sha256="missing")


def test_observation_keeps_unknowns_and_never_publishes_qualified_output():
    observation = Observation(
        name="electronic excitation energy", value=4.1, unit="eV",
        actual_conditions={"method": "PBE0", "basis": "def2-TZVP"},
        sources=[ObservationSource(artifact_id="output", sha256=DIGEST, location="lines 21-25")],
        code_sha256="b" * 64, limitations=["No dedicated response-state checker"],
    )
    result = Result(run_id="run", operation_status="completed", observation_records=[observation])
    assert result.qualified_outputs == {}
    assert result.observation_records[0].validation_status == "unverified"
    unknown = Observation(name="field", value=1)
    assert unknown.unit is unknown.code_sha256 is None
    assert unknown.sources == []
    with pytest.raises(ValidationError):
        Observation(name="field", value=1, validation_status="passed")
    with pytest.raises(ValidationError):
        Observation(name="field", value={"nested": [float("nan")]})


def test_internal_receipts_are_owned_by_run_and_keep_unknown_execution():
    receipt = InternalCall(request_key="candidate_1", input_fingerprint=DIGEST,
                           code=CodeRef(artifact_id="program", sha256=DIGEST),
                           inputs=InputManifest(entries=[entry()]), state="unknown")
    run = Run(request_id="request", request_version=1, internal_calls=[receipt])
    loaded = Run.model_validate_json(run.model_dump_json())
    assert loaded.internal_calls == [receipt]
    assert loaded.internal_calls[0].result_id is None
    assert loaded.usage.orca_starts_actual == 0


def test_legacy_contracts_do_not_acquire_generic_scientific_success(tmp_path):
    old_goal = Goal.model_validate_json('{"id":"energy","port":"energy"}')
    assert old_goal.minimum_check_version == "orca-hf-1"
    assert old_goal.evidence_contract_version == "legacy-1"
    assert old_goal.verification == "qualified"
    old_bytes = b'{"run_id":"run","operation_status":"completed","observations":{"field":1}}'
    path = tmp_path / "old-result.json"
    path.write_bytes(old_bytes)
    old_result = Result.model_validate_json(path.read_bytes())
    assert old_result.observation_records == []
    assert old_result.qualified_outputs == {}
    assert path.read_bytes() == old_bytes


def test_historical_result_consumption_fingerprint_remains_valid(tmp_path):
    # Frozen full pre-code-contract Result JSON, including its original defaults.
    old_bytes = (
        b'{"artifact_ids":["raw"],"attempt_id":"attempt","call_id":null,"checks":{},'
        b'"created_at":"2026-10-17T00:00:00Z","diagnostics":[],"id":"result_historical",'
        b'"observations":{"energy":-75.0},"operation_status":"completed","qualified_outputs":{},'
        b'"run_id":"run_historical","source":{},"step_id":"sp","supersedes_result_id":null}'
    )
    path = tmp_path / "result_historical.json"
    path.write_bytes(old_bytes)
    result = Result.model_validate_json(path.read_bytes())
    expected = "05a48d15435b1ce6c54b2b562825229998787e5958ada9e2bc37f71901bdb64c"
    assert fingerprint(result) == expected
    assert fingerprint(Result.model_validate_json(result.model_dump_json())) == expected
    result.observation_records.append(Observation(name="excitation energy", value=4.1, unit="eV"))
    assert fingerprint(result) != expected
    result.observation_records.clear()
    result.observations["energy"] = -76.0
    assert fingerprint(result) != expected
    assert path.read_bytes() == old_bytes


def test_historical_delivery_receipt_keeps_its_snapshot_identity_after_loading(tmp_path):
    source = Path(__file__).parents[1] / "fixtures" / "code_contracts" / "legacy_delivery.json"
    original = source.read_bytes()
    path = tmp_path / "historical-delivery.json"
    path.write_bytes(original)
    data = json.loads(path.read_bytes())
    request = Request.model_validate(data["request"])
    run = Run.model_validate(data["run"])
    result = Result.model_validate(data["result"])
    selections = {"inspect": {"result": result, "assessment": data["assessment"],
                              "source_integrity": data["source_integrity"]}}
    expected = "366d823f41e55f4e9d5347b87618094e8b02f1a4787a689b90450227d0c8e81e"
    current = delivery_snapshot(request, run, selections)
    assert current == data["snapshot"]
    assert current["fingerprint"] == run.terminal_deliveries[0].snapshot_fingerprint == expected
    assert run.terminal_deliveries[0].contract_version == "terminal-delivery-1"
    assert path.read_bytes() == original

    run.permission.code_execution = True
    assert delivery_snapshot(request, run, selections)["fingerprint"] != expected
    run.permission.code_execution = False
    result.observation_records.append(Observation(name="additional field", value=1))
    assert delivery_snapshot(request, run, selections)["fingerprint"] != expected
    assert path.read_bytes() == original
