"""Actual staged bytes, source preservation and condition conflicts; no processes."""

import hashlib
import json
from types import SimpleNamespace

import pytest

from orca_agent.applicability import effective_conditions
from orca_agent.models import (
    CodeRef,
    Goal,
    InputManifestEntry,
    NativeOrcaParameters,
    OutputBinding,
    Plan,
    Request,
    ResourceLimits,
    Step,
)
from orca_agent.orca.native import prepare_native_input, validate_native_input
from orca_agent.orca.native_profile import profile_contract
from tests.unit.test_native_managed import POLICY, XYZ, bound, source


def prepare(tmp_path, text, *, simplified=True):
    inp, xyz = tmp_path / "original.inp", tmp_path / "original.xyz"
    inp.write_bytes(text.encode("ascii"))
    xyz.write_bytes(XYZ)
    parameters = NativeOrcaParameters(
        input=CodeRef(artifact_id="input", sha256=hashlib.sha256(inp.read_bytes()).hexdigest(),
                      language="orca"),
        inputs={"entries": [InputManifestEntry(
            artifact_id="geometry", sha256=hashlib.sha256(XYZ).hexdigest(),
            target_path="initial.xyz", purpose="geometry")]})
    binding = bound()
    if simplified:
        binding["workflow_policy"] = "simplified-1"
    return prepare_native_input(
        tmp_path / "attempt", parameters, {"input": inp, "geometry": xyz}.__getitem__,
        ResourceLimits(), policy=POLICY, binding=binding)


def test_omitted_authorized_controls_are_in_actual_input_and_source_is_preserved(tmp_path):
    original = "! TPSSh def2-TZVP SP\n* xyzfile 0 5 initial.xyz\n"
    manifest = prepare(tmp_path, original)
    final = (tmp_path / "attempt/job.inp").read_bytes()
    assert b"UKS" in final and b"NoAutoStart" in final
    assert b"%pal nprocs 4 end" in final and b"%maxcore 192" in final
    assert (tmp_path / "attempt/source-input.inp").read_bytes() == original.encode()
    assert (tmp_path / "original.inp").read_bytes() == original.encode()
    assert manifest["source_input_sha256"] == hashlib.sha256(original.encode()).hexdigest()
    assert manifest["input_sha256"] == hashlib.sha256(final).hexdigest()
    assert manifest["source_input_sha256"] != manifest["input_sha256"]
    assert manifest["actual_conditions"]["electronic_state"] == "UKS"
    assert manifest["resources"]["cores"] == 4
    persisted = json.loads((tmp_path / "attempt/input-manifest.json").read_bytes())
    assert persisted["materialization"] == manifest["materialization"]
    assert persisted["actual_conditions"]["scientific_validation"] == "unverified"


def test_explicit_controls_are_not_overridden_and_unmodified_bytes_keep_original_hash(tmp_path):
    text = source().replace("%pal nprocs 4", "%pal nprocs 2")
    manifest = prepare(tmp_path, text)
    assert manifest["resources"]["cores"] == 2
    assert (tmp_path / "attempt/job.inp").read_bytes() == text.encode()
    assert "materialization" not in manifest and "source_input_sha256" not in manifest


@pytest.mark.parametrize("change", [
    lambda s: s.replace(" UKS ", " RKS "),
    lambda s: s.replace("%pal nprocs 4", "%pal nprocs 8"),
    lambda s: s.replace("%maxcore 192", "%maxcore 500"),
])
def test_explicit_conflicting_or_excess_controls_remain_rejected(tmp_path, change):
    with pytest.raises(ValueError):
        prepare(tmp_path, change(source()))
    assert not (tmp_path / "attempt/job.inp").exists()


def test_historical_missing_state_is_not_silently_upgraded(tmp_path):
    with pytest.raises(ValueError, match="wavefunction"):
        prepare(tmp_path, source().replace(" UKS ", " "), simplified=False)


def test_missing_iteration_controls_are_materialized_without_changing_explicit_values(tmp_path):
    text = source(job="Opt", blocks="%geom TolE 1e-6 end").replace(
        "%scf MaxIter 200 AutoTRAH false end", "%scf MaxIter 200 end")
    manifest = prepare(tmp_path, text)
    blocks = manifest["actual_conditions"]["input_blocks"]
    assert blocks["scf"] == {"autotrah": "false", "maxiter": "200"}
    assert blocks["geom"]["maxiter"] == str(POLICY.max_geom_iterations)
    assert manifest["execution_impact"]["scf_maxiter"] == 200
    assert manifest["execution_impact"]["geom_maxiter"] == POLICY.max_geom_iterations


@pytest.mark.parametrize("controls,expected", [
    ("NMR true", {"nmr": "true"}),
    ("Nuclei all", {"nuclei": "all"}),
    ("Nuclei 0", {"nuclei": "0"}),
    ("NMR false Nuclei 0", {"nmr": "false", "nuclei": "0"}),
])
def test_known_property_block_order_is_actionable_before_execution(tmp_path, controls, expected):
    with pytest.raises(ValueError, match="eprnmr must follow"):
        prepare(tmp_path, source(blocks=f"%eprnmr {controls} end"))
    assert not (tmp_path / "attempt/job.inp").exists()
    text = source() + f"%eprnmr {controls} end\n"
    manifest = prepare(tmp_path, text)
    assert manifest["actual_conditions"]["input_blocks"]["eprnmr"] == expected
    assert (tmp_path / "attempt/job.inp").read_bytes() == text.encode()
    assert manifest["input_sha256"] == hashlib.sha256(text.encode()).hexdigest()
    assert "materialization" not in manifest


def test_non_nuclear_property_controls_do_not_add_an_order_gate(tmp_path):
    manifest = prepare(tmp_path, source(blocks="%eprnmr NMR false end"))
    assert manifest["actual_conditions"]["input_blocks"]["eprnmr"]["nmr"] == "false"


def test_actual_a004_nuclei_order_failure_is_rejected_before_staging():
    # Exact settled a004/step_ed3720508cc5466eb0e31389de3639a3/attempt-001
    # source-input.inp and stderr.txt bytes. No ORCA or model is called here.
    original = (b"! PBE0 def2-TZVP RKS NMR NoAutoStart\n%maxcore 192\n"
                b"%scf MaxIter 256 AutoTRAH false end\n%eprnmr\n  Nuclei = all\nend\n"
                b"* xyzfile 0 1 hcn.xyz\n")
    stderr = (b"[file orca_main/main_read_eprnmr.cpp, line 237]: Error in [EPRNMR] block: "
              b"nuclear properties are requested but no coordinates have been read!\n"
              b"Please put the coordinates input before the [EPRNMR] block in your input file!\n\n")
    assert hashlib.sha256(original).hexdigest() == "6fd07e60e1f7372dd97291be82d9802e2d4760dd657ba4c09eccaca27807563a"
    assert hashlib.sha256(stderr).hexdigest() == "861dcf5b151f46b783c12519ce3910f4d4e368cb22a9b789c726777cabf415f7"
    with pytest.raises(ValueError, match="Nuclei requests nuclear properties"):
        validate_native_input(original.decode(), {"hcn.xyz": XYZ}, ResourceLimits(), policy=POLICY,
                              binding={**bound(), "workflow_policy": "simplified-1"})
    contract = profile_contract(POLICY)["geometry"]
    assert "before" in contract and "%eprnmr" in contract and "Nuclei" in contract
    assert "atom index 0" in contract and "not automatically reordered" in contract


def test_authorized_environment_variants_do_not_permit_arbitrary_solvents():
    binding = {**bound(), "workflow_policy": "simplified-1",
               "allowed_condition_values": {"environment": ["gas_phase", "cpcm(water)"]}}
    result = validate_native_input(source(solvent="CPCM(Water)"), {"initial.xyz": XYZ},
                                   ResourceLimits(), policy=POLICY, binding=binding)
    assert result["actual_conditions"]["environment"] == "cpcm(water)"
    with pytest.raises(ValueError, match="environment"):
        validate_native_input(source(solvent="CPCM(DMSO)"), {"initial.xyz": XYZ},
                              ResourceLimits(), policy=POLICY, binding=binding)


def test_explicit_goal_environment_overrides_default_only_in_new_request():
    goal = Goal(id="water", port="energy", conditions={"environment": "CPCM(water)"})
    request = Request(
        goals=[goal],
        conditions={"environment": "gas_phase"},
        conditions_source={"environment": "default"},
        condition_evidence={"workflow_policy": {"version": "simplified-1",
                                               "source": "run_configuration"}})
    result = effective_conditions(request, goal)
    assert result["conditions"]["environment"] == "CPCM(water)"
    assert "conflicting_goal_condition:environment" not in result["reasons"]
    request.conditions_source["environment"] = "user"
    assert "conflicting_goal_condition:environment" in effective_conditions(request, goal)["reasons"]
    request.conditions_source["environment"] = "default"
    request.condition_evidence.clear()
    assert "conflicting_goal_condition:environment" in effective_conditions(request, goal)["reasons"]


def test_a_water_only_step_cannot_execute_the_global_gas_default(monkeypatch):
    from orca_agent.tools import managed

    request = Request(method="TPSSh", basis="def2-TZVP", charge=0, multiplicity=5,
        conditions={"environment": "gas_phase", "electronic_state": "UKS"},
        conditions_source={"environment": "default"},
        goals=[Goal(id="water", port="artifacts", conditions={"environment": "CPCM(water)"},
                    minimum_check_version="generic-observation-1",
                    evidence_contract_version="generic-observation-1",
                    verification="reported_with_limitations")])
    step = Step(id="water_sp", logical_id="water_sp", tool="orca.run",
                parameters={"input": {"artifact_id": "code", "sha256": "a" * 64,
                                      "language": "orca"}})
    plan = Plan(request_id=request.id, steps=[step],
                goal_map={"water": OutputBinding(step_id=step.id, port="artifacts")})
    configuration = SimpleNamespace(workflow_policy="simplified-1",
                                    execution=SimpleNamespace(native_input_policy=POLICY))
    store = SimpleNamespace(run_configuration=lambda _: configuration,
                            load_request=lambda _: request, load_plan=lambda _: plan)
    monkeypatch.setattr(managed, "_initial_geometry", lambda *a: (
        "geometry", SimpleNamespace(sha256=hashlib.sha256(XYZ).hexdigest()), XYZ))
    binding = managed.native_input_binding(store, object(), step)
    assert binding["conditions"]["environment"] == "cpcm(water)"
    assert binding["condition_sources"]["environment"] == "goal:water"
    assert "allowed_condition_values" not in binding
    with pytest.raises(ValueError, match="environment"):
        validate_native_input(source(), {"initial.xyz": XYZ}, ResourceLimits(),
                              policy=POLICY, binding=binding)
    validate_native_input(source(solvent="CPCM(water)"), {"initial.xyz": XYZ},
                          ResourceLimits(), policy=POLICY, binding=binding)
