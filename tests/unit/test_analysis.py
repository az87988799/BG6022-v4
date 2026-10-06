"""Scientific counterexamples and frozen independent sampling references.

All executions here are offline. Synthetic source records test the consumption
boundary, while frozen numerical references test actual analysis behavior.
"""

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from orca_agent.models import Check, QualifiedOutput, Result
from orca_agent.orca.checks import check_outputs
from orca_agent.tools.analysis import (
    AnalysisMember,
    EnergyCompareParameters,
    EnergyConditions,
    EnergyEvidence,
    SamplingCandidate,
    SamplingParameters,
    bind_energy,
    candidate_geometry,
    energy_compare,
    finite_sampling,
    sampling_check_contract,
)

PROJECT = Path(__file__).resolve().parents[2]
MANIFEST = json.loads((PROJECT / "tests/fixtures/phase_b/sampling-candidates.json").read_text())
REVIEW = json.loads((PROJECT / "docs/acceptance/phase-b/reference-review.json").read_text())


def passed_checks():
    return [Check(name=c.name, status="passed", rule_version="orca-hf-2")
            for c in check_outputs({}, "orca.sp")["energy"]]


def energy(candidate_id="first", value=-75.0, digest="a" * 64, **changes):
    data = {
        "run_id": f"run_{candidate_id}", "attempt_id": f"attempt_{candidate_id}",
        "result_id": f"result_{candidate_id}", "tool": "orca.sp", "energy_eh": value,
        "artifact_hashes": {f"geometry_{candidate_id}": digest, f"stdout_{candidate_id}": "b" * 64},
        "geometry_artifact_id": f"geometry_{candidate_id}", "geometry_sha256": digest,
        "elements": ["O", "H", "H"], "atom_mapping": ["0:O", "1:H", "2:H"],
        "conditions": EnergyConditions(method="HF", basis="STO-3G", charge=0, multiplicity=1,
                                       electronic_state="RHF", environment="gas_phase"),
        "checks": passed_checks(),
    }
    data.update(changes)
    return EnergyEvidence(**data)


def compare(first=None, second=None, **parameters):
    return energy_compare([
        AnalysisMember(id="A", evidence=first or energy()),
        AnalysisMember(id="B", evidence=second or energy("second", -74.9)),
    ], EnergyCompareParameters(member_a="A", member_b="B", **parameters))


def test_energy_difference_sign_formula_and_both_explicit_sources():
    pair = REVIEW["source_index"]["phase-b/compatible-water-pair"]
    first, second = [energy(key, pair[key]["raw_observation"]["energy_eh"],
                            pair[key]["geometry"]["sha256"]) for key in ("A", "B")]
    result = compare(first, second, allow_different_geometries=True)
    output = result["qualified_outputs"]["energy_difference"]
    assert output["value"] == pytest.approx(pair["expected_difference_eh"], abs=1e-12)
    assert output["value"] < 0
    assert output["unit"] == "Eh"
    assert output["source"]["A"]["result_id"] == first.result_id
    assert output["source"]["B"]["artifact_hashes"] == second.artifact_hashes
    assert output["checks"][0]["rule_version"] == "energy-compare-1"
    assert "not a free energy" in result["limitation"]


def test_different_geometry_requires_the_explicit_request_relation():
    result = compare(second=energy("second", digest="c" * 64))
    assert result["reason"] == "geometry_difference_not_authorized"
    assert result["qualified_outputs"] == {}


@pytest.mark.parametrize("quantity", ["free_energy", "enthalpy", "activation_free_energy"])
def test_electronic_energy_cannot_complete_a_different_quantity(quantity):
    result = compare(quantity=quantity)
    assert result["reason"] == "requested_quantity_not_supported"
    assert not result["qualified_outputs"]


@pytest.mark.parametrize("changes,reason", [
    ({"elements": ["C", "H", "H", "H", "H"], "atom_mapping": ["0:C", "1:H", "2:H", "3:H", "4:H"]},
     "incompatible_system_or_atom_mapping"),
    ({"atom_mapping": ["0:O", "2:H", "1:H"]}, "incompatible_system_or_atom_mapping"),
    ({"conditions": EnergyConditions()}, "missing_required_conditions"),
    ({"conditions": EnergyConditions(method="HF", basis="STO-3G", charge=0, multiplicity=1,
                                      electronic_state="RHF", environment="water_solvent")},
     "incompatible_scientific_conditions"),
])
def test_same_energy_unit_does_not_establish_comparability(changes, reason):
    result = compare(second=energy("second", **changes))
    assert result["reason"] == reason
    assert result["qualified_outputs"] == {}


def test_required_missing_member_blocks_complete_collection_optional_does_not():
    members = [AnalysisMember(id="A", evidence=energy()),
               AnalysisMember(id="B", evidence=energy("second", -74.9)),
               AnalysisMember(id="C", missing_reason="SCF not converged")]
    parameters = EnergyCompareParameters(member_a="A", member_b="B")
    incomplete = energy_compare(members, parameters)
    assert incomplete["reason"] == "missing_required_member"
    assert incomplete["members"][-1]["missing_reason"] == "SCF not converged"
    assert not incomplete["qualified_outputs"]
    members[-1].required = False
    partial_optional = energy_compare(members, parameters)
    assert partial_optional["scientific_status"] == "passed"
    assert partial_optional["members"][-1]["status"] == "missing"


def test_collection_limits_and_member_identity_are_checked():
    parameters = EnergyCompareParameters(member_a="A", member_b="B")
    for members in ([AnalysisMember(id="A")] * 2,
                    [AnalysisMember(id=f"m{x}") for x in range(6)]):
        with pytest.raises(ValueError, match="one to five uniquely"):
            energy_compare(members, parameters)


@pytest.mark.parametrize("change", ["old_rule", "failed", "missing_checks", "geometry_hash"])
def test_unqualified_sources_never_become_analysis_inputs(change):
    source = energy().model_dump()
    if change == "old_rule":
        source["checks"][0]["rule_version"] = "orca-hf-1"
    elif change == "failed":
        source["checks"][0]["status"] = "failed"
    elif change == "missing_checks":
        source["checks"] = source["checks"][:-1]
    else:
        source["geometry_sha256"] = "d" * 64
    with pytest.raises(ValueError):
        EnergyEvidence.model_validate(source)


def sampling_case(window_id, extra=None):
    window = next(w for w in MANIFEST["windows"] if w["id"] == window_id)
    source = MANIFEST["source"]
    parameters = SamplingParameters(
        target_width_angstrom=window["target_width_angstrom"],
        energy_threshold_eh=REVIEW["sampling"]["threshold_eh"],
        fixed_bond_angstrom=source["r02_angstrom"], fixed_angle_degrees=source["angle_degrees"],
    )
    candidates, members, geometries = [], [], {}
    for item in window["candidates"]:
        candidate = SamplingCandidate(
            id=item["id"], artifact_id=f"geometry_{item['id']}", sha256=item["sha256"],
            declared_r_angstrom=item["declared_r_angstrom"], required_initial=item["required_initial"],
        )
        candidates.append(candidate)
        geometries[candidate.artifact_id] = (PROJECT / item["path"]).read_bytes()
        record = REVIEW["sampling"]["raw_records"][item["id"]]
        evidence = None
        if item["required_initial"] or item["id"] == extra:
            evidence = energy(item["id"], record["raw_observation"]["energy_eh"], item["sha256"])
        members.append(AnalysisMember(id=item["id"], required=item["required_initial"], evidence=evidence))
    return candidates, members, geometries, parameters


@pytest.mark.parametrize("window_id", ["left", "right", "stop"])
def test_actual_xyz_and_frozen_reference_yield_expected_discrete_judgment(window_id):
    expected = next(w for w in REVIEW["sampling"]["windows"] if w["window_id"] == window_id)
    arguments = sampling_case(window_id)
    initial = finite_sampling(*arguments)
    assert initial["goal_satisfied"] == expected["initial"]["satisfied"]
    assert initial["reason"] == expected["initial"]["reason"]
    assert initial["neighbor_span_angstrom"] == pytest.approx(
        expected["initial"]["neighbor_span_angstrom"], abs=1e-12,
    )
    assert initial["sampled_candidate_ids"] == [c.id for c in arguments[0] if c.required_initial]
    assert "selected_action" not in initial and "direction" not in initial
    assert len(initial["members"]) == 5
    if window_id != "stop":
        assert not initial["qualified_outputs"]
        final = finite_sampling(*sampling_case(window_id, expected["selected_candidate_id"]))
        assert final["goal_satisfied"]
        assert final["minimum_candidate_id"] == expected["after_action"]["minimum_candidate_id"]
        assert final["qualified_outputs"]["sampling"]["checks"][0]["rule_version"] == "finite-sampling-1"
    else:
        assert initial["qualified_outputs"]["sampling"]["unit"] == "angstrom"


def test_left_and_right_observations_change_relative_neighbor_energies_without_selecting_action():
    left = finite_sampling(*sampling_case("left"))
    right = finite_sampling(*sampling_case("right"))
    assert left["left_gap_eh"] < left["right_gap_eh"]
    assert right["right_gap_eh"] < right["left_gap_eh"]


def test_sampling_contract_uses_effective_tolerance_and_inclusive_span_bound():
    candidates, members, geometries, parameters = sampling_case("stop")
    span = finite_sampling(candidates, members, geometries, parameters)["neighbor_span_angstrom"]
    parameters.distance_tolerance_angstrom = 1e-7
    parameters.target_width_angstrom = span - parameters.distance_tolerance_angstrom
    boundary = finite_sampling(candidates, members, geometries, parameters)
    assert boundary["goal_satisfied"]
    assert boundary["acceptance_criteria"] == sampling_check_contract(parameters)
    assert boundary["acceptance_criteria"]["distance_tolerance_angstrom"] == 1e-7
    assert sampling_check_contract()["default_distance_tolerance_angstrom"] == 1e-8
    assert "span<=target_width_angstrom+distance_tolerance_angstrom" in boundary["acceptance_criteria"]["rule"]
    parameters.target_width_angstrom = span - 2 * parameters.distance_tolerance_angstrom
    outside = finite_sampling(candidates, members, geometries, parameters)
    assert not outside["goal_satisfied"] and outside["reason"] == "span_too_wide"


def test_sampling_distinctness_compares_each_other_energy_with_minimum_not_every_pair():
    candidates, members, geometries, parameters = sampling_case("stop")
    required = [member for member in members if member.required]
    required[0].evidence.energy_eh = required[-1].evidence.energy_eh = -74.0
    result = finite_sampling(candidates, members, geometries, parameters)
    assert result["goal_satisfied"]
    assert "each other sampled E-Emin>energy_threshold_eh" in result["acceptance_criteria"]["rule"]


@pytest.mark.parametrize("variant,reason", [
    ("missing", "missing_required_energy"),
    ("boundary", "boundary_minimum"),
    ("tie", "not_numerically_distinct"),
    ("wrong_geometry", "energy_geometry_binding_mismatch"),
    ("opt", "energy_geometry_binding_mismatch"),
])
def test_honest_partial_sampling_for_missing_boundary_degeneracy_or_wrong_source(variant, reason):
    candidates, members, geometries, parameters = sampling_case("stop")
    required = [m for m in members if m.required]
    if variant == "missing":
        required[0].evidence = None
    elif variant == "boundary":
        required[0].evidence.energy_eh = -76
    elif variant == "tie":
        required[0].evidence.energy_eh = required[1].evidence.energy_eh + parameters.energy_threshold_eh / 2
    elif variant == "wrong_geometry":
        required[0].evidence = energy("alien", -75)
    else:
        required[0].evidence.tool = "orca.opt"
    result = finite_sampling(candidates, members, geometries, parameters)
    assert not result["goal_satisfied"] and not result["qualified_outputs"]
    assert result["reason"] == reason


@pytest.mark.parametrize("variant,message", [
    ("hash", "hash mismatch"), ("label", "scan distance"),
    ("fixed_bond", "fixed bond"), ("angle", "fixed angle"),
    ("mapping", "atom mapping"),
])
def test_labels_and_hashes_do_not_replace_actual_geometry_constraints(variant, message):
    candidates, _, geometries, parameters = sampling_case("stop")
    candidate = candidates[0]
    data = geometries[candidate.artifact_id]
    if variant == "hash":
        data += b"\n"
    elif variant == "label":
        candidate.declared_r_angstrom += 0.1
    elif variant == "fixed_bond":
        rows = data.decode().splitlines()
        values = rows[4].split()
        values[1] = str(float(values[1]) + 0.1)
        rows[4] = " ".join(values)
        data = ("\n".join(rows) + "\n").encode()
        candidate.sha256 = hashlib.sha256(data).hexdigest()
    elif variant == "angle":
        # Rotate the fixed bond while preserving its length and the scanned r.
        rows = data.decode().splitlines()
        o = [float(x) for x in rows[2].split()[1:]]
        h = [float(x) for x in rows[4].split()[1:]]
        rows[4] = "H " + " ".join(str(2 * a - b) for a, b in zip(o, h, strict=True))
        data = ("\n".join(rows) + "\n").encode()
        candidate.sha256 = hashlib.sha256(data).hexdigest()
    else:
        rows = data.decode().splitlines()
        rows[2], rows[3] = rows[3], rows[2]
        data = ("\n".join(rows) + "\n").encode()
        candidate.sha256 = hashlib.sha256(data).hexdigest()
    with pytest.raises(ValueError, match=message):
        candidate_geometry(candidate, data, parameters)


def test_invalid_optional_geometry_cannot_be_silently_ignored():
    candidates, members, geometries, parameters = sampling_case("stop")
    geometries[candidates[1].artifact_id] += b"changed"
    result = finite_sampling(candidates, members, geometries, parameters)
    assert result["reason"] == "invalid_candidate_geometry"
    assert candidates[1].id in result["invalid_candidates"]
    assert not result["goal_satisfied"]


def test_forged_required_membership_cannot_reduce_minimum_evidence():
    candidates, members, geometries, parameters = sampling_case("left")
    members[0].required = False
    with pytest.raises(ValueError, match="immutable goal"):
        finite_sampling(candidates, members, geometries, parameters)


@pytest.mark.parametrize("field", ["target_width_angstrom", "energy_threshold_eh", "fixed_bond_angstrom"])
@pytest.mark.parametrize("value", [0, -1, float("nan"), float("inf")])
def test_scientific_thresholds_must_be_finite_positive(field, value):
    parameters = sampling_case("stop")[-1].model_dump()
    parameters[field] = value
    with pytest.raises(ValueError):
        SamplingParameters.model_validate(parameters)


@pytest.fixture
def bound_store(tmp_path):
    """Minimal read-only storage double; no mock parser or scientific executor."""
    water = (PROJECT / MANIFEST["windows"][0]["candidates"][0]["path"]).read_bytes()
    files, artifacts, paths = {}, {}, {}
    for index, (name, data) in enumerate([
        ("stdout.out", b"immutable original evidence"), ("job.inp", b"! RHF STO-3G TightSCF"),
        ("geometry.xyz", water),
    ]):
        artifact_id = f"artifact_{index}"
        digest = hashlib.sha256(data).hexdigest()
        path = tmp_path / name
        path.write_bytes(data)
        files[name] = {"artifact_id": artifact_id, "sha256": digest}
        artifacts[artifact_id] = SimpleNamespace(id=artifact_id, sha256=digest,
                                                 run_id="run_one", attempt_id="attempt_one")
        paths[artifact_id] = path
    checks = passed_checks()
    result = Result(id="result_one", run_id="run_one", step_id="step_one", attempt_id="attempt_one",
                    operation_status="completed", artifact_ids=list(artifacts), checks={"energy": checks},
                    qualified_outputs={"energy": QualifiedOutput(value=-75, unit="Eh", checks=checks)},
                    source={"files": files, "conditions": {
                        "method": "HF", "basis": "STO-3G", "charge": 0, "multiplicity": 1,
                    }, "input_fingerprint": "frozen_input", "geometry_artifact_id": "initial_geometry"})
    run = SimpleNamespace(result_ids=[result.id], attempts=[SimpleNamespace(
        id="attempt_one", step_id="step_one", tool="orca.sp", input_fingerprint="frozen_input",
        geometry_artifact_id="initial_geometry",
    )])
    verified = []

    def artifact_path(artifact_id):
        verified.append(artifact_id)
        if hashlib.sha256(paths[artifact_id].read_bytes()).hexdigest() != artifacts[artifact_id].sha256:
            raise ValueError("artifact hash changed")
        return paths[artifact_id]

    return SimpleNamespace(load_run=lambda _: run, load_result=lambda *_: result,
                           load_artifact=artifacts.__getitem__, artifact_path=artifact_path,
                           result=result, run=run, paths=paths, artifacts=artifacts, verified=verified)


def test_binding_reads_exact_source_and_rechecks_each_hash_on_every_consumption(bound_store):
    first = bind_energy(bound_store, "run_one", "result_one", expected_attempt_id="attempt_one")
    assert first.attempt_id == "attempt_one" and first.energy_eh == -75
    assert set(bound_store.verified) == set(bound_store.artifacts)
    assert first.conditions.environment == "gas_phase"
    bound_store.paths["artifact_0"].write_bytes(b"changed after successful read")
    with pytest.raises(ValueError, match="hash changed"):
        bind_energy(bound_store, "run_one", "result_one")


@pytest.mark.parametrize("variant", [
    "wrong_attempt", "legacy_rule", "unknown", "unbound", "unqualified", "missing_condition",
    "artifact_provenance", "incomplete_manifest", "checks_disagree", "unconverged_opt",
    "wrong_input_fingerprint", "wrong_initial_geometry",
])
def test_binding_rejects_ambiguous_historical_unqualified_or_incomplete_sources(bound_store, variant):
    result = bound_store.result
    kwargs = {}
    if variant == "wrong_attempt":
        kwargs["expected_attempt_id"] = "attempt_other"
    elif variant == "legacy_rule":
        result.checks["energy"][0].rule_version = "orca-hf-1"
    elif variant == "unknown":
        result.operation_status = "unknown"
    elif variant == "unbound":
        bound_store.run.result_ids = []
    elif variant == "unqualified":
        result.qualified_outputs = {}
    elif variant == "missing_condition":
        result.source["conditions"].pop("basis")
    elif variant == "artifact_provenance":
        bound_store.artifacts["artifact_0"].attempt_id = "attempt_other"
    elif variant == "incomplete_manifest":
        result.artifact_ids.append("unlisted")
    elif variant == "checks_disagree":
        result.checks["energy"] = result.checks["energy"][:-1]
    elif variant == "wrong_input_fingerprint":
        result.source["input_fingerprint"] = "another_input"
    elif variant == "wrong_initial_geometry":
        result.source["geometry_artifact_id"] = "another_geometry"
    else:
        bound_store.run.attempts[0].tool = "orca.opt"
    with pytest.raises(ValueError):
        bind_energy(bound_store, "run_one", "result_one", **kwargs)


def test_inplace_record_mutation_cannot_smuggle_nan_into_successful_sampling():
    candidates, members, geometry, parameters = sampling_case("stop")
    members[0].evidence.energy_eh = float("nan")
    with pytest.raises(ValueError):
        finite_sampling(candidates, members, geometry, parameters)


def test_inplace_parameter_mutation_cannot_make_infinite_width_pass():
    candidates, members, geometry, parameters = sampling_case("left")
    parameters.target_width_angstrom = float("inf")
    with pytest.raises(ValueError):
        finite_sampling(candidates, members, geometry, parameters)
