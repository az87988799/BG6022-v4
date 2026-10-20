"""Managed scientific scope, execution controls, and historical compatibility."""

import hashlib

import pytest

from orca_agent.models import NativeInputPolicy, ResourceLimits
from orca_agent.orca.native import validate_native_input
from orca_agent.orca.native_profile import canonical_native_condition, profile_contract

XYZ = b"3\nheavy open shell example\nFe 0 0 0\nCl 0 0 2\nCl 0 0 -2\n"
POLICY = NativeInputPolicy(profile="managed-native-1", max_atoms=1024)


def bound(data=XYZ, **changes):
    return {"request_version": 1, "system_id": "iron", "geometry_sha256": hashlib.sha256(data).hexdigest(),
        "conditions": {"method": "TPSSh", "basis": "def2-TZVP", "charge": 0, "multiplicity": 5,
                       "electronic_state": "UKS", "environment": "gas_phase", **changes}}


def source(job="SP", blocks="", *, method="TPSSh", basis="def2-TZVP", state="UKS", mult=5, solvent=""):
    return (f"! {method} {basis} {state} {job} {solvent} TightSCF NoAutoStart\n"
        "%maxcore 192\n%pal nprocs 4 end\n%scf MaxIter 200 AutoTRAH false end\n"
        + blocks + f"\n* xyzfile 0 {mult} initial.xyz\n")


def admit(text=None, *, data=XYZ, binding=None, policy=POLICY):
    return validate_native_input(source() if text is None else text, {"initial.xyz": data}, ResourceLimits(),
        policy=policy, binding=bound(data) if binding is None else binding)


def test_new_profile_does_not_narrow_scientific_method_basis_element_or_spin():
    actual = admit()["actual_conditions"]
    assert actual["method"] == "TPSSh" and actual["basis"] == "def2-TZVP"
    assert actual["multiplicity"] == 5 and actual["electronic_state"] == "UKS"
    assert actual["scientific_validation"] == "unverified"
    text = source(method="FutureMethod", basis="FutureBasis", blocks="%method UnknownCoefficient 0.15 end")
    actual = admit(text, binding=bound(method="FutureMethod", basis="FutureBasis"))["actual_conditions"]
    assert actual["input_blocks"]["method"]["unknowncoefficient"] == "0.15"


@pytest.mark.parametrize("job,blocks,run_type", [
    ("Opt", "%geom MaxIter 7 TolE 1e-6 end", "optimization"),
    ("Freq", "%freq Temp 298.15,310 end", "analytic_frequencies"),
    ("AnFreq", "", "analytic_frequencies"),
    ("NumFreq", "%freq CentralDiff true Increment 0.005 end", "numerical_frequencies"),
    ("Opt NumFreq", "%geom MaxIter 7 end", "optimization_and_numerical_frequencies"),
])
def test_independent_derivative_and_optimization_execution_is_accounted(job, blocks, run_type):
    result = admit(source(job, blocks))
    assert result["actual_conditions"]["run_type"] == run_type
    assert result["execution_impact"]["internal_work_units"] > 1
    if "NumFreq" in job:
        assert result["execution_impact"]["derivative_slots"] == 19**2


def test_constrained_multidimensional_relaxed_scan_has_explicit_work_product():
    result = admit(source("Opt", """%geom MaxIter 3
        Constraints { B 0 1 2.0 C } { C 2 C } end
        Scan B 0 1 = 1.8, 2.2, 4
             A 1 0 2 = 160, 180, 3 end
        end"""))
    assert result["actual_conditions"]["run_type"] == "relaxed_scan"
    assert result["execution_impact"]["scan_points"] == 12
    assert result["execution_impact"]["optimization_evaluation_slots"] == 48


def test_wrong_constraint_arity_identifies_record_and_supported_dihedral_shape():
    # Actual a015 failure shape; admission must explain it without rewriting it.
    bad = source("Opt", "%geom MaxIter 3 Constraints { B 2 1 3 4 60 C } end end")
    with pytest.raises(ValueError, match="unsupported geometry constraint shape") as caught:
        admit(bad)
    detail = str(caught.value)
    assert "B requires exactly 2 zero-based atom indices" in detail
    assert "D a four-atom dihedral" in detail
    assert "received { B 2 1 3 4 60 C }" in detail

    # A separately authored valid shape stays literal and gains no scientific qualification.
    xyz = b"4\nconstraint syntax fixture\nC 0 0 0\nC 1 0 0\nC 1 1 0\nC 1 1 1\n"
    good = source("Opt", "%geom MaxIter 3 Constraints { D 0 1 2 3 60 C } end end")
    actual = admit(good, data=xyz)["actual_conditions"]
    assert actual["input_blocks"]["geom"]["constraints"] == ["{", "D", "0", "1", "2", "3", "60", "C", "}"]
    assert actual["scientific_validation"] == "unverified"


def test_tddft_response_is_separately_finite_with_open_scientific_scalars():
    result = admit(source(blocks="%tddft NRoots 7 MaxIter 50 TDA true DoSOC true end"))
    assert result["execution_impact"]["response_roots"] == 7
    assert result["execution_impact"]["internal_work_units"] == 8


def test_block_frequency_selector_cannot_bypass_numerical_work_reservation():
    result = admit(source(job="", blocks="%freq NumFreq true end"))
    assert result["actual_conditions"]["run_type"] == "numerical_frequencies"
    assert result["execution_impact"]["derivative_slots"] == 361
    with pytest.raises(ValueError, match="competing job"):
        admit(source(job="SP", blocks="%freq NumFreq true end"))
    with pytest.raises(ValueError, match="disagree"):
        admit(source(job="Freq", blocks="%freq AnFreq false end"))
    with pytest.raises(ValueError, match="competing job"):
        admit(source(job="Freq", blocks="%freq NumFreq true end"))


def test_other_response_roots_are_also_bounded_and_counted():
    result = admit(source(blocks="%casscf NRoots 2,3 MaxIter 30 end"))
    assert result["execution_impact"]["response_roots"] == 5
    with pytest.raises(ValueError, match="frozen bound"):
        admit(source(blocks="%mdci NRoots 129 end"))


@pytest.mark.parametrize("solvent,blocks,environment", [
    ("CPCM(Water)", "", "CPCM(water)"),
    ("SMD(DMSO)", "", "SMD(dmso)"),
    ("", '%cpcm SMD true SMDsolvent "dimethyl sulfoxide" end', "SMD(dimethyl sulfoxide)"),
    ("", "%cpcm Epsilon 80.4 Refrac 1.33 end", "CPCM(epsilon=80.4)"),
])
def test_solvent_conditions_are_parsed_from_input(solvent, blocks, environment):
    result = admit(source(solvent=solvent, blocks=blocks), binding=bound(environment=environment))
    assert canonical_native_condition("environment", result["actual_conditions"]["environment"], POLICY) == (
        canonical_native_condition("environment", environment, POLICY))


def test_hf_alias_and_composite_method_do_not_need_scientific_enumeration():
    hf = source(method="HF", state="UHF")
    assert admit(hf, binding=bound(method="HF", electronic_state="UHF"))["actual_conditions"]["method"] == "HF"
    alias = hf.replace("HF def2-TZVP UHF", "UHF def2-TZVP")
    assert admit(alias, binding=bound(method="HF", electronic_state="UHF"))["actual_conditions"]["method"] == "HF"
    composite = source(method="r2SCAN-3c", basis="")
    result = admit(composite, binding=bound(method="r2SCAN-3c", basis="built-in"))
    assert result["actual_conditions"]["basis"] == "built-in"
    alias = source(method="B973c", basis="")
    assert admit(alias, binding=bound(method="B97-3c", basis="built-in"))["actual_conditions"]["basis"] == "built-in"
    post_hf = source(method="CCSD(T)", state="RHF").replace("CCSD(T) def2-TZVP RHF", "RHF CCSD(T) def2-TZVP")
    assert admit(post_hf, binding=bound(method="CCSD(T)", electronic_state="RHF"))["actual_conditions"]["method"] == "CCSD(T)"
    unknown = post_hf.replace("CCSD(T)", "FutureMethod")
    assert admit(unknown, binding=bound(method="FutureMethod", electronic_state="RHF"))["actual_conditions"]["method"] == "FutureMethod"


def test_only_explicit_single_temperature_has_a_scalar_actual_condition():
    result = admit(source(job="Freq", blocks="%freq Temp 298.15 end"))
    assert result["actual_conditions"]["temperature_K"] == 298.15
    assert result["actual_conditions"]["input_blocks"]["freq"]["temp"] == "298.15"
    multiple = admit(source(job="Freq", blocks="%freq Temp 298.15,310 end"))["actual_conditions"]
    assert multiple["temperatures_K"] == [298.15, 310.0] and "temperature_K" not in multiple
    absent = admit(source(job="Freq"))["actual_conditions"]
    assert "temperature_K" not in absent and "standard_state" not in absent
    for value in ("0", "-1", "inf"):
        with pytest.raises(ValueError):
            admit(source(job="Freq", blocks=f"%freq Temp {value} end"))


def test_large_atom_count_is_a_frozen_configurable_limit_not_32_atom_cap():
    data = ("40\nexplicit\n" + "Xe 0 0 0\n" * 40).encode()
    assert admit(data=data)["actual_conditions"]["atom_count"] == 40
    with pytest.raises(ValueError, match="atom limit"):
        admit(data=data, policy=NativeInputPolicy(profile="managed-native-1", max_atoms=39))


@pytest.mark.parametrize("field,value", [("method", "PBE"), ("basis", "STO-3G"),
    ("charge", 1), ("multiplicity", 3), ("electronic_state", "RKS"), ("environment", "CPCM(water)")])
def test_no_script_or_request_claim_can_override_explicit_input(field, value):
    with pytest.raises(ValueError, match="Request/Step condition"):
        admit(binding=bound(**{field: value}))


@pytest.mark.parametrize("addition", ["ExtOpt", "MRCC", "GOAT", "NEB", "Autostart", "MORead",
    "Extrapolate(2)", "PAL8", "NumGrad", "QMMM", "XTB2", "OptTS"])
def test_unaccounted_external_or_workflow_control_is_refused(addition):
    with pytest.raises(ValueError, match="managed|unmanaged"):
        admit(source(job=addition))


@pytest.mark.parametrize("blocks", [
    '%scf GuessFile "secrets.gbw" end', "%geom MaxIter 3 InHess Read end",
    "%geom MaxIter 3 Calc_Hess true end", "%freq Restart true end",
    '%method External "C:/tool.exe" end', "%md Timestep 0.5 end",
    "%unknownscience Foo 3 end", "%pal NProcs 8 end",
    "%tddft NRoots 4 end", "%tddft NRoots 400 MaxIter 20 end",
    "%geom MaxIter 3 Scan B 0 1 = 1,2 end end",
    "%geom MaxIter 3 Scan B 0 4 = 1,2,3 end end",
    "%geom MaxIter 3 Constraints { B 0 9 C } end end",
    "%geom MaxIter 3 Scan B 0 1 = 1,2,0 end end",
    "%method Functional PBE end", "%basis Basis STO-3G end",
    "%output JsonGbwFile true end", "%freq Temp nan end",
])
def test_dependency_control_resource_and_ambiguous_scientific_overrides_fail_closed(blocks):
    with pytest.raises(ValueError):
        admit(source("Opt", blocks))


def test_unknown_solvent_is_not_silently_called_gas_phase():
    with pytest.raises(ValueError, match="no explicit solvent"):
        admit(source(blocks="%cpcm Refrac 1.33 end"))
    with pytest.raises(ValueError, match="disagree"):
        admit(source(solvent="CPCM(Water)", blocks='%cpcm Solvent "ethanol" end'))
    with pytest.raises(ValueError, match="SMD true"):
        admit(source(blocks='%cpcm SMD false SMDsolvent "water" end'))
    with pytest.raises(ValueError, match="disagree"):
        admit(source(solvent="SMD(Water)", blocks="%cpcm SMD false end"))
    with pytest.raises(ValueError, match="NoCPCM conflicts"):
        admit(source(solvent="CPCM(Water) NoCPCM"), binding=bound(environment="CPCM(water)"))
    with pytest.raises(ValueError, match="explicit solvent"):
        admit(source(solvent="CPCM"))


def test_draco_auxiliary_execution_is_not_disguised_as_scalar_solvent_control():
    with pytest.raises(ValueError, match="managed execution/dependency contract"):
        admit(source(blocks='%cpcm solvent "water" DRACO true draco_charges ceh end'),
              binding=bound(environment="CPCM(water)"))
    for keyword in ("COSMORS(Water)", "DRACO", "ECRISM"):
        with pytest.raises(ValueError, match="managed.*control"):
            admit(source(job="SP " + keyword))
    for keyword in ("COSMO(Water)", "GBSA(Water)", "SMD18"):
        with pytest.raises(ValueError, match="environment-condition contract"):
            admit(source(job="SP " + keyword))


def test_wavefunction_and_method_override_cannot_hide_in_open_scientific_fields():
    text = source().replace("AutoTRAH false", "AutoTRAH false HFTyp RHF")
    with pytest.raises(ValueError, match="HFTyp conflicts"):
        admit(text)
    text = source(method="HF", state="UHF", blocks="%method Method DFT end")
    with pytest.raises(ValueError, match="selector conflicts"):
        admit(text, binding=bound(method="HF", electronic_state="UHF"))
    for selector in ("PBE", "CCSD(T)", "STO-3G"):
        with pytest.raises(ValueError, match="competing.*selectors"):
            admit(source(job="SP " + selector))
    # OPI recognition is not an eligibility whitelist for additional ordinary
    # scientific controls; ORCA owns validity of new spellings.
    result = admit(source(job="SP FutureScientificModifier"))
    assert "FutureScientificModifier" in result["actual_conditions"]["input_keywords"]


def test_historical_profile_shape_and_caps_remain_frozen():
    assert NativeInputPolicy().model_dump(mode="json") == {
        "profile": "bounded-analytic-1", "max_atoms": 32, "max_geom_iterations": 128,
        "max_scf_iterations": 256, "max_response_iterations": 128}
    for field, value in (("max_atoms", 33), ("max_geom_iterations", 129),
                         ("max_scf_iterations", 257), ("max_response_iterations", 129)):
        with pytest.raises(ValueError, match="historical"):
            NativeInputPolicy(**{field: value})
        assert getattr(NativeInputPolicy(profile="managed-native-1", **{field: value}), field) == value
    assert profile_contract(POLICY)["admission_version"] == "native-orca-input-3"
    assert profile_contract(NativeInputPolicy())["admission_version"] == "native-orca-input-2"
    assert canonical_native_condition("method", "TPSSh") is None
    assert canonical_native_condition("method", "TPSSh", POLICY) == "tpssh"


def test_legacy_without_policy_retains_original_admission():
    text = "! UnknownMethod UnknownBasis NoAutoStart\n%maxcore 192\n* xyzfile 0 5 initial.xyz\n"
    assert validate_native_input(text, {"initial.xyz": XYZ}, ResourceLimits())["admission_version"] == "native-orca-input-1"
