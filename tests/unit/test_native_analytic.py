"""Admission and budget negatives; no ORCA, Docker or scientific qualification."""

import base64
import hashlib
import time

import pytest

from orca_agent.config import Config
from orca_agent.models import (
    Attempt,
    BudgetLimits,
    CodeRef,
    ExecutionConfiguration,
    Goal,
    InputManifest,
    InputManifestEntry,
    NativeInputPolicy,
    NativeOrcaParameters,
    OutputBinding,
    PermissionSnapshot,
    Plan,
    Request,
    ResourceLimits,
    RunConfiguration,
    Step,
    fingerprint,
)
from orca_agent.orca.native import prepare_native_input, validate_native_input
from orca_agent.store import BudgetExceeded, Store, StoreError
from orca_agent.tools import managed
from orca_agent.tools.managed import native_input_binding

XYZ = b"2\nregistered hydrogen\nH 0 0 0\nH 0 0 0.74\n"
INPUT = """! RHF STO-3G TightSCF NORI NoAutoStart Opt Freq
%maxcore 192
%pal nprocs 4 end
%scf MaxIter 100 ConvForced true AutoTRAH false end
%geom MaxIter 4 EnforceStrictConvergence true end
%method Z_MaxIter 64 end
* xyzfile 0 1 initial.xyz
"""
POLICY = NativeInputPolicy()


def sha(data):
    return hashlib.sha256(data).hexdigest()


def binding(**conditions):
    return {"request_id": "request", "request_version": 1, "step_id": "native", "system_id": None,
            "geometry_sha256": sha(XYZ), "conditions": {
                "method": "HF", "basis": "STO-3G", "charge": 0, "multiplicity": 1,
                "electronic_state": "RHF", "environment": "gas_phase", **conditions}}


def admit(text=INPUT, *, data=XYZ, policy=POLICY, bound=None):
    return validate_native_input(text, {"initial.xyz": data}, ResourceLimits(),
                                 policy=policy, binding=binding() if bound is None else bound)


def test_combined_analytic_input_records_input_origin_and_conservative_slots():
    result = admit()
    assert result["admission_version"] == "native-orca-input-2"
    assert result["actual_conditions_origin"] == "admitted_native_input"
    assert result["actual_conditions"]["origin"] == "admitted_native_input"
    assert result["actual_conditions"]["input_geometry_sha256"] == sha(XYZ)
    assert result["actual_conditions"]["request_version"] == 1
    assert result["actual_conditions"]["run_type"] == "optimization_and_analytic_frequencies"
    assert result["actual_conditions"]["scientific_validation"] == "unverified"
    impact = result["execution_impact"]
    assert impact["internal_work_units"] == 12  # (MaxIter+1) + Hessian + 3N
    assert (impact["scf_maxiter"], impact["response_maxiter"]) == (100, 64)
    assert "electronic_evaluation_upper_bound" not in impact


@pytest.mark.parametrize("method", ["BP86", "PBE", "BLYP", "PBE0", "B3LYP", "B3LYP/G"])
def test_documented_scf_derivative_compatibility_is_not_a_general_method_whitelist(method):
    result = admit(INPUT.replace("RHF STO-3G", method + " RKS def2-SVP"),
                   bound=binding(method=method, basis="def2-SVP", electronic_state="RKS"))
    assert result["actual_conditions"]["method"] == method
    # The historical profile still accepts unknown single-point method labels.
    old = "! NewMethod NewBasis NoAutoStart\n%maxcore 192\n* xyzfile 0 1 initial.xyz\n"
    assert validate_native_input(old, {"initial.xyz": XYZ}, ResourceLimits())["admission_version"].endswith("-1")


@pytest.mark.parametrize("before,after", [
    ("RHF", "MP2 RKS"), ("RHF", "B2PLYP RKS"), ("RHF", "Unknown RKS"),
    ("RHF", "PBE0 RHF"), ("RHF", "HF RKS"), ("RHF", "UHF"),
    ("RHF", "B3LYP"),  # No implicit restricted-state admission.
    ("NORI", "RIJK"), ("NORI", "NoCOSX"), ("NORI", "NORI RIJCOSX"),
    ("Freq", "NumFreq"), ("Opt Freq", "Freq"), ("Opt Freq", "Opt"),
    ("Opt Freq", "Opt AnFreq Freq"), ("Opt", "OptTS"), ("Opt", "Opt Scan"),
    ("TightSCF", "TightSCF LooseSCF"), ("NoAutoStart", "NoAutoStart AutoStart"),
    ("NoAutoStart", "NoAutoStart BOHRS"), ("NoAutoStart", "NoAutoStart CPCM(Water)"),
    ("MaxIter 4", "MaxIter 129"), ("MaxIter 100", "MaxIter 257"),
    ("Z_MaxIter 64", "Z_MaxIter 129"), ("Z_MaxIter 64", "Z_MaxIter 64 Z_MaxIter 32"),
    ("AutoTRAH false", "AutoTRAH true"), ("AutoTRAH false", ""),
    ("ConvForced true", "ConvForced false"), ("EnforceStrictConvergence true", "EnforceStrictConvergence false"),
    ("%method Z_MaxIter 64 end", "%method Z_MaxIter 64 Z_Solver CG end"),
    ("%geom MaxIter 4", "%geom CalcHess true MaxIter 4"),
    ("%method Z_MaxIter 64 end", "%freq Temp 298 end"),
    ("* xyzfile 0 1", "* xyzfile 0 3"), ("* xyzfile 0 1", "* xyzfile 1 1"),
    ("* xyzfile 0 1 initial.xyz", "* xyzfile 0 1 initial.xyz\n*"),
])
def test_unknown_ambiguous_or_unbounded_derivative_syntax_fails_closed(before, after):
    with pytest.raises(ValueError):
        admit(INPUT.replace(before, after))


@pytest.mark.parametrize("field,value", [("method", "PBE"), ("basis", "def2-SVP"),
    ("charge", 2), ("multiplicity", 3), ("electronic_state", "RKS"), ("environment", "water_solvent")])
def test_declared_script_conditions_cannot_replace_current_request(field, value):
    with pytest.raises(ValueError, match="Request/Step condition"):
        admit(bound=binding(**{field: value}))


def test_same_atoms_or_translated_geometry_is_not_same_prepared_artifact():
    with pytest.raises(ValueError, match="xyzfile bytes"):
        admit(data=XYZ.replace(b"0.74", b"0.75"))
    with pytest.raises(ValueError, match="xyzfile bytes"):
        admit(data=XYZ.replace(b"registered hydrogen", b"unbound comment"))
    with pytest.raises(ValueError, match="atom limit"):
        admit(policy=NativeInputPolicy(max_atoms=1))
    with pytest.raises(ValueError, match="pinned xyzfile"):
        validate_native_input(INPUT.replace("* xyzfile 0 1 initial.xyz", "* xyz 0 1\nH 0 0 0\nH 0 0 .74\n*"),
                              {}, ResourceLimits(), policy=POLICY, binding=binding())


def test_legacy_configuration_and_attempt_serialization_preserve_exact_shape():
    old = ExecutionConfiguration().model_dump(mode="json")
    assert "native_input_policy" not in old
    assert fingerprint(old) == fingerprint(ExecutionConfiguration.model_validate(old))
    old_attempt = Attempt(step_id="s", logical_id="s", number=1, tool="orca.run",
                          input_fingerprint="old", directory="old").model_dump(mode="json")
    assert "native_workload" not in old_attempt
    assert fingerprint(old_attempt) == fingerprint(Attempt.model_validate(old_attempt))
    assert RunConfiguration(version=1).execution.native_input_policy is None
    with pytest.raises(ValueError, match="Linux"):
        ExecutionConfiguration(native_input_policy=POLICY)


def setup_native(tmp_path, monkeypatch, *, units=12, tool="orca.run", policy=POLICY):
    monkeypatch.setattr("orca_agent.store.require_execution_available", lambda _: None)
    execution = ExecutionConfiguration(backend="linux-container", image_id="sha256:" + "a" * 64,
        manifest_sha256="b" * 64, docker_context="desktop-linux", docker_path="E:/docker.exe",
        native_input_policy=policy)
    store = Store(tmp_path / "data", environment_root=tmp_path / "environment",
                  configuration=RunConfiguration(execution=execution))
    source, geometry_path = tmp_path / "input.inp", tmp_path / "initial.xyz"
    source.write_bytes(INPUT.encode())
    geometry_path.write_bytes(XYZ)
    code, geometry = store.import_artifact(source, "native_input"), store.import_artifact(geometry_path, "geometry")
    request = Request(geometry_artifact_id=geometry.id,
        conditions={"electronic_state": "RHF", "environment": "gas_phase"},
        goals=[Goal(id="raw", port="artifacts", minimum_check_version="generic-observation-1",
                    evidence_contract_version="generic-observation-1", verification="reported_with_limitations")])
    params = NativeOrcaParameters(input=CodeRef(artifact_id=code.id, sha256=code.sha256, language="orca"),
        inputs=InputManifest(entries=[InputManifestEntry(artifact_id=geometry.id, sha256=geometry.sha256,
                                                        target_path="initial.xyz", purpose="geometry")]))
    if tool == "python.execute":
        parameters = {"code": {"artifact_id": code.id, "sha256": code.sha256, "language": "python"}}
    else:
        parameters = params
    step = Step(id="native", logical_id="native", tool=tool, parameters=parameters)
    plan = Plan(request_id=request.id, steps=[step], goal_map={"raw": OutputBinding(step_id=step.id, port="artifacts")})
    run = store.create_run(request, plan, PermissionSnapshot(allowed_tools=["orca.run", "python.execute"],
        scientific_execution=True, code_execution=True, artifact_writes=True, artifact_ids=[code.id, geometry.id]),
        BudgetLimits(orca_starts=2, extra_orca_starts=1, internal_work_units=units))
    prepared = prepare_native_input(tmp_path / "staged", params, store.artifact_path, ResourceLimits(),
        policy=policy, binding=native_input_binding(store, run, step))
    return store, run, step, prepared, params


def test_task_candidate_geometry_requires_both_frozen_policy_and_trusted_binding():
    policy = POLICY.model_copy(update={"allow_task_geometry": True})
    bound = {**binding(), "allow_task_geometry": True, "initial_atom_symbols": ["H", "H"]}
    changed = XYZ.replace(b"0.74", b"0.75")
    result = admit(data=changed, policy=policy, bound=bound)
    facts = result["actual_conditions"]
    assert result["input_binding"]["geometry_sha256"] == sha(XYZ)
    assert facts["input_geometry_sha256"] == sha(changed)
    assert facts["initial_geometry_sha256"] == sha(XYZ)
    assert facts["input_geometry_relation"] == "task_generated_candidate"
    assert facts["scientific_validation"] == "unverified"
    for denied_policy, denied_binding in ((POLICY, bound), (policy, binding())):
        with pytest.raises(ValueError, match="xyzfile bytes"):
            admit(data=changed, policy=denied_policy, bound=denied_binding)
    with pytest.raises(ValueError, match="ordered elements"):
        admit(data=changed.replace(b"H 0 0 0", b"He 0 0 0").replace(b"H 0 0 0.75", b"He 0 0 0.75"),
              policy=policy, bound=bound)


@pytest.mark.parametrize("tool,restricted", [("python.execute", False), ("orca.run", False),
                                           ("python.execute", True)])
def test_only_unrestricted_python_receives_candidate_geometry_binding(tmp_path, monkeypatch, tool, restricted):
    policy = POLICY.model_copy(update={"allow_task_geometry": True})
    store, run, step, _, _ = setup_native(tmp_path, monkeypatch, tool=tool, policy=policy)
    if restricted:
        request = store.load_request(run)
        request.goals[0].conditions["geometry_relation"] = "fixed_initial"
        original = store.load_request
        monkeypatch.setattr(store, "load_request", lambda current: request if current.id == run.id else original(current))
    bound = native_input_binding(store, run, step)
    assert bound.get("allow_task_geometry", False) is (tool == "python.execute" and not restricted)
    if bound.get("allow_task_geometry"):
        assert bound["initial_atom_symbols"] == ["H", "H"]


def test_direct_reservation_is_atomic_idempotent_and_survives_reopen(tmp_path, monkeypatch):
    store, run, step, prepared, _ = setup_native(tmp_path, monkeypatch)
    attempt = store.reserve_attempt(run, step)
    store.reserve_native_workload(run, attempt, prepared)
    assert run.usage.internal_work_units == 12 and run.usage.orca_starts_reserved == 1
    persisted = store.load_run(run.id)
    assert persisted.attempts[0].native_workload == attempt.native_workload
    reopened = Store(store.root, environment_root=store.environment_root)
    reopened.reserve_native_workload(persisted, persisted.attempts[0], prepared)
    assert persisted.usage.internal_work_units == 12
    changed = {**prepared, "execution_impact": {**prepared["execution_impact"], "internal_work_units": 1}}
    with pytest.raises(StoreError, match="differs"):
        reopened.reserve_native_workload(persisted, persisted.attempts[0], changed)
    persisted.attempts[0].state = "unknown"
    reopened.save_run(persisted)
    with pytest.raises(StoreError, match="unstarted"):
        reopened.reserve_native_workload(persisted, persisted.attempts[0], prepared)
    assert reopened.environment_lease() is not None


def test_underbudget_refused_before_native_workload_or_any_actual_start(tmp_path, monkeypatch):
    store, run, step, prepared, _ = setup_native(tmp_path, monkeypatch, units=11)
    attempt = store.reserve_attempt(run, step)
    with pytest.raises(BudgetExceeded, match="work unit"):
        store.reserve_native_workload(run, attempt, prepared)
    assert store.load_run(run.id).usage.internal_work_units == 0
    assert attempt.native_workload is None and run.usage.orca_starts_actual == 0


def test_sdk_reserves_work_once_without_direct_attempt_charge(tmp_path, monkeypatch):
    store, run, step, prepared, params = setup_native(tmp_path, monkeypatch, tool="python.execute")
    attempt = store.reserve_attempt(run, step)
    with pytest.raises(StoreError, match="direct Attempt"):
        store.reserve_native_workload(run, attempt, prepared)
    receipt = store.reserve_internal_call(run, attempt, "sdk_job", "f" * 64, code=params.input,
        inputs=params.inputs, conditions={"execution_impact": prepared["execution_impact"]},
        work_units=prepared["execution_impact"]["internal_work_units"])
    assert receipt.work_units == run.usage.internal_work_units == 12
    assert run.usage.orca_starts_reserved == 1 and attempt.native_workload is None
    with pytest.raises(StoreError, match="reconcile"):
        store.reserve_internal_call(run, attempt, "sdk_job", "f" * 64, code=params.input,
                                    inputs=params.inputs, work_units=12)
    assert store.load_run(run.id).usage.internal_work_units == 12


@pytest.mark.parametrize("case", ["unknown_method", "wrong_geometry", "forged_conditions", "work_budget"])
def test_broker_checks_admitted_bytes_and_binding_before_any_native_start(tmp_path, monkeypatch, case):
    store, run, step, _, _ = setup_native(tmp_path, monkeypatch,
        units=11 if case == "work_budget" else 12, tool="python.execute")
    attempt = store.reserve_attempt(run, step)
    (store.path(attempt.directory) / "sealed").mkdir()
    monkeypatch.setattr(managed.local, "check_active_job", lambda _: None)
    monkeypatch.setattr(managed, "launch_orca", lambda *a, **k: pytest.fail("native process boundary reached"))
    config = Config(execution=run.configuration.execution)
    broker = managed._Broker(store, run, step, attempt, config, None,
        tmp_path / "requests", tmp_path / "responses", time.monotonic() + 15,
        {"workdir": tmp_path / "broker-native"})
    value = {"version": 1, "request_key": "bounded", "input": INPUT,
             "dependencies": [{"target_path": "initial.xyz", "content_base64": base64.b64encode(XYZ).decode()}]}
    if case == "unknown_method":
        value["input"] = INPUT.replace("RHF", "UnregisteredFunctional RKS")
    elif case == "wrong_geometry":
        value["dependencies"][0]["content_base64"] = base64.b64encode(XYZ.replace(b"0.74", b"0.75")).decode()
    elif case == "forged_conditions":
        value["actual_conditions"] = binding()["conditions"]
    with pytest.raises(ValueError):
        broker.execute(value, {"job_name": "offline-no-process"})
    loaded = store.load_run(run.id)
    assert not loaded.internal_calls and loaded.usage.internal_work_units == 0
    assert loaded.usage.orca_starts_reserved == loaded.usage.orca_starts_actual == 0


def test_workload_cannot_be_added_or_rewritten_without_atomic_charge(tmp_path, monkeypatch):
    store, run, step, prepared, _ = setup_native(tmp_path, monkeypatch)
    attempt = store.reserve_attempt(run, step)
    attempt.native_workload = {"execution_impact": {"internal_work_units": 12}}
    with pytest.raises(StoreError, match="atomically charged"):
        store.save_run(run)
    assert store.load_run(run.id).attempts[0].native_workload is None
    run = store.load_run(run.id)
    store.reserve_native_workload(run, run.attempts[0], prepared)
    run.attempts[0].native_workload["execution_impact"]["internal_work_units"] = 1
    with pytest.raises(StoreError, match="cannot be rewritten"):
        store.save_run(run)
    assert store.load_run(run.id).usage.internal_work_units == 12
