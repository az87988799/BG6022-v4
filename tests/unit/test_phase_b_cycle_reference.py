"""Independent Opt reference replay; no ORCA or model execution."""

from pathlib import Path

import pytest

from tests.helpers.phase_b_budget import reference

FIXTURE = Path(__file__).parents[1] / "fixtures/phase_b/independent/methane-opt-reference"


def test_opt_reference_binds_actual_last_stage_and_xyz():
    actual = reference.independent_optimization_output(FIXTURE / "stdout.out", FIXTURE / "job.xyz",
                                                       ["C", "H", "H", "H", "H"])
    assert actual["status"] == "converged"
    assert actual["final_stage_line"] < actual["scf_converged_line"] < actual["energy_line"]
    assert actual["optimized_geometry_sha256"] == reference.sha256_file(FIXTURE / "job.xyz")
    assert set(actual["strict_convergence"]) == {"Energy change", "RMS gradient", "MAX gradient", "RMS step", "MAX step"}


@pytest.mark.parametrize("change", ["later_cycle", "later_final", "loose", "no", "geometry", "mapping"])
def test_prior_converged_opt_never_qualifies_changed_final_stage_or_geometry(tmp_path, change):
    text = (FIXTURE / "stdout.out").read_text()
    xyz = (FIXTURE / "job.xyz").read_bytes()
    mapping = ["C", "H", "H", "H", "H"]
    if change == "later_cycle":
        text += "\nGEOMETRY OPTIMIZATION CYCLE 99\n"
    elif change == "later_final":
        text += "\nFINAL ENERGY EVALUATION AT THE STATIONARY POINT\n"
    elif change == "loose":
        text = text.replace("0.0000300000", "0.0030000000")
    elif change == "no":
        start = text.rfind("Geometry convergence")
        text = text[:start] + text[start:].replace("YES", "NO", 1)
    elif change == "geometry":
        lines = xyz.decode().splitlines()
        atom = lines[3].split()
        atom[1] = str(float(atom[1]) + 0.01)
        lines[3] = " ".join(atom)
        xyz = ("\n".join(lines) + "\n").encode()
    else:
        mapping = ["H", "C", "H", "H", "H"]
    output, geometry = tmp_path / "stdout.out", tmp_path / "job.xyz"
    output.write_text(text)
    geometry.write_bytes(xyz)
    assert reference.independent_optimization_output(output, geometry, mapping)["status"] == "unverified"


def test_opt_is_explicit_and_sp_frozen_source_shape_remains_unchanged(tmp_path):
    geometry = tmp_path / "geometry.xyz"
    geometry.write_bytes((FIXTURE / "job.xyz").read_bytes())
    path = tmp_path / "reference.inp"
    path.write_text(reference.reference_input(100))
    sources = reference.reviewed_sources(geometry, path, 100, atom_mapping=["C", "H", "H", "H", "H"])
    assert "job_type" not in sources
    path.write_text(reference.reference_input(100, job_type="opt"))
    with pytest.raises(ValueError, match="input differs"):
        reference.reviewed_sources(geometry, path, 100)
    sources = reference.reviewed_sources(geometry, path, 100, job_type="opt", atom_mapping=["C", "H", "H", "H", "H"])
    assert sources["job_type"] == "opt"
    assert sources["parameters"]["opt_maxiter"] == 100
    assert sources["parameters"]["timeout_seconds"] == 120
    assert "EnforceStrictConvergence true" in path.read_text()


@pytest.mark.parametrize("stage_rule", [None, "wrong-version", "optimization-final-stage-1"])
def test_new_joint_contract_requires_versioned_stage_but_legacy_replay_is_unchanged(stage_rule):
    from orca_agent.models import Check, QualifiedOutput, Result
    from tests.helpers import phase_b_grade_joint as grade
    raw_path, xyz = FIXTURE / "stdout.out", FIXTURE / "job.xyz"
    raw = reference.independent_output(raw_path)
    source = {"path": xyz.relative_to(grade.PROJECT).as_posix(), "sha256": reference.sha256_file(xyz)}
    files = [{"path": p.relative_to(grade.PROJECT).as_posix(), "sha256": reference.sha256_file(p)} for p in (raw_path, xyz)]
    review = {"source_index": {"phase-a/methane_opt": {"energy": {"reference_eh": raw["energy_eh"], "tolerance_eh": 1e-7},
               "geometry": {"tolerance_angstrom": 1e-5}, "files": files}}}
    cases = {"common": {"fixed_input_files": {"methane_opt": source}}}
    checks = [Check(name=n, status="passed", rule_version="orca-hf-2") for n in grade.GEOMETRY_CHECKS]
    if stage_rule:
        checks.append(Check(name="optimization_stage_binding", status="passed", rule_version="orca-hf-2",
                            source={"rule_version": stage_rule}))
    result = Result(run_id="synthetic_run", operation_status="completed", source={"files": {"job.xyz": {"artifact_id": "final_xyz"}}},
        checks={"optimized_geometry": checks}, qualified_outputs={"optimized_geometry": QualifiedOutput(artifact_id="final_xyz", checks=checks)})
    evidence = [{"geometry_sha256": source["sha256"], "raw": raw, "result": result,
                 "files": {"job.xyz": xyz, "stdout.out": raw_path}}]
    facts = {}
    grade._grade_single("methane_opt_control", evidence, review, cases, lambda n, p, *args: facts.update({n: bool(p)}), current_contract=True)
    assert all(facts.values()) is (stage_rule == "optimization-final-stage-1")
    if stage_rule is None:
        legacy = {}
        grade._grade_single("methane_opt_control", evidence, review, cases, lambda n, p, *args: legacy.update({n: bool(p)}))
        assert all(legacy.values())
        assert "current_final_optimization_stage" not in legacy
