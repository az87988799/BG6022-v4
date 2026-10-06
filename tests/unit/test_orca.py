"""Parser regression: marked synthetic specimens and a traced real output subset."""

import hashlib
import json
import re
import shutil
from pathlib import Path

import pytest

from orca_agent.models import CalculationParameters
from orca_agent.orca.adapter import prepare_input, read_outputs

FIXTURES = Path(__file__).parents[1] / "fixtures" / "phase_a"


def snapshot(directory):
    return {p.name: hashlib.sha256(p.read_bytes()).hexdigest()
            for p in directory.iterdir() if p.is_file()}


def synthetic_output(geometry, *, converged=True, opt=False, normal=True):
    coords = "\n".join(geometry.read_text().splitlines()[2:])
    result = (
        "Synthetic unit-test specimen; not a calculation.\n"
        "Program Version 6.1.1\n"
        "Your calculation utilizes the basis: STO-3G\n"
        "Hartree-Fock type HFTyp .... RHF\n"
        "Total Charge Charge .... 0\n"
        "Multiplicity Mult .... 1\n"
        "CARTESIAN COORDINATES (ANGSTROEM)\n------------------------\n"
        + coords + "\n\n"
        + ("SCF CONVERGED AFTER 9 CYCLES\n" if converged else "SCF NOT CONVERGED\n")
        + "FINAL SINGLE POINT ENERGY -75.123456789000\n"
    )
    if opt:
        result += (
            "Geometry convergence\n"
            "Energy change 0.0000001 0.0000010 YES\n"
            "RMS gradient 0.000001 0.000030 YES\n"
            "MAX gradient 0.000003 0.000100 YES\n"
            "RMS step 0.000010 0.000600 YES\n"
            "MAX step 0.000030 0.001000 YES\n"
            "THE OPTIMIZATION HAS CONVERGED\n"
        )
    if normal:
        result += "ORCA TERMINATED NORMALLY\n"
    return result


def prepare_case(tmp_path, name="water_sp"):
    tool = "orca.opt" if "opt" in name else "orca.sp"
    params = CalculationParameters()
    prepare_input(tmp_path, FIXTURES / name / "geometry.xyz", params, tool)
    return params, tool


def test_opi_input_fresh_and_no_execution(tmp_path):
    source = FIXTURES / "water_opt" / "geometry.xyz"
    before = source.read_bytes()
    prepare_case(tmp_path, "water_opt")
    text = (tmp_path / "job.inp").read_text().lower()
    assert all(word in text for word in ("rhf", "sto-3g", "tightscf", "tightopt", "convforced 1"))
    assert "geometry.xyz" in text and "%maxcore 192" in text
    assert "enforcestrictconvergence true" in text
    assert source.read_bytes() == before
    with pytest.raises(FileExistsError):
        prepare_case(tmp_path, "water_opt")


def test_sp_explicit_text_path_is_read_only(tmp_path):
    params, tool = prepare_case(tmp_path)
    (tmp_path / "stdout.out").write_text(synthetic_output(tmp_path / "geometry.xyz"))
    before = snapshot(tmp_path)
    result = read_outputs(tmp_path, params, tool)
    assert result["qualified_outputs"]["energy"]["unit"] == "Eh"
    assert result["observations"]["energy_source"] == "stdout.out"
    assert snapshot(tmp_path) == before
    assert not (tmp_path / "job.property.json").exists()


@pytest.mark.parametrize("replacement", [
    ("SCF CONVERGED AFTER 9 CYCLES", "SCF NOT CONVERGED"),
    ("ORCA TERMINATED NORMALLY", "aborted"),
    ("FINAL SINGLE POINT ENERGY -75.123456789000", "FINAL SINGLE POINT ENERGY nan"),
    ("Multiplicity Mult .... 1", "Multiplicity Mult .... 3"),
    ("Program Version 6.1.1", "Program Version 6.0.1"),
    ("Program Version 6.1.1", "Program Version 6.1.1-f.1"),
    ("0.757160", "0.957160"),
])
def test_failure_cannot_publish_energy(tmp_path, replacement):
    params, tool = prepare_case(tmp_path)
    text = synthetic_output(tmp_path / "geometry.xyz").replace(*replacement)
    (tmp_path / "stdout.out").write_text(text)
    assert not read_outputs(tmp_path, params, tool)["qualified_outputs"]


def test_input_tamper_fails(tmp_path):
    params, tool = prepare_case(tmp_path)
    (tmp_path / "stdout.out").write_text(synthetic_output(tmp_path / "geometry.xyz"))
    with (tmp_path / "job.inp").open("a") as stream:
        stream.write("\n! B3LYP\n")
    assert not read_outputs(tmp_path, params, tool)["qualified_outputs"]


def test_echoed_or_concatenated_output_cannot_grant_success(tmp_path):
    params, tool = prepare_case(tmp_path)
    text = synthetic_output(tmp_path / "geometry.xyz")
    (tmp_path / "stdout.out").write_text(text + text)
    assert not read_outputs(tmp_path, params, tool)["qualified_outputs"]
    text = text.replace("SCF CONVERGED AFTER", "| 1> # SCF CONVERGED AFTER")
    (tmp_path / "stdout.out").write_text(text)
    assert not read_outputs(tmp_path, params, tool)["qualified_outputs"]


def test_opt_partial_energy_without_qualified_structure(tmp_path):
    params, tool = prepare_case(tmp_path, "water_opt")
    (tmp_path / "stdout.out").write_text(synthetic_output(tmp_path / "geometry.xyz"))
    (tmp_path / "job.xyz").write_bytes((tmp_path / "geometry.xyz").read_bytes())
    result = read_outputs(tmp_path, params, tool)
    assert "energy" in result["qualified_outputs"]
    assert "optimized_geometry" not in result["qualified_outputs"]
    assert result["observations"]["energy_geometry"]["atoms"]


def test_opt_requires_matching_final_geometry_and_threshold_table(tmp_path):
    params, tool = prepare_case(tmp_path, "water_opt")
    text = synthetic_output(tmp_path / "geometry.xyz", opt=True)
    (tmp_path / "stdout.out").write_text(text)
    (tmp_path / "job.xyz").write_bytes((tmp_path / "geometry.xyz").read_bytes())
    assert "optimized_geometry" in read_outputs(tmp_path, params, tool)["qualified_outputs"]
    (tmp_path / "stdout.out").write_text(text.replace("0.000003 0.000100 YES", "0.000300 0.000100 NO"))
    assert "optimized_geometry" not in read_outputs(tmp_path, params, tool)["qualified_outputs"]


def test_opt_cannot_reuse_prior_convergence_table(tmp_path):
    params, tool = prepare_case(tmp_path, "water_opt")
    text = synthetic_output(tmp_path / "geometry.xyz", opt=True).replace(
        "THE OPTIMIZATION HAS CONVERGED", "Geometry convergence\nTHE OPTIMIZATION HAS CONVERGED"
    )
    (tmp_path / "stdout.out").write_text(text)
    (tmp_path / "job.xyz").write_bytes((tmp_path / "geometry.xyz").read_bytes())
    assert "optimized_geometry" not in read_outputs(tmp_path, params, tool)["qualified_outputs"]


def test_invalid_json_never_silently_falls_back(tmp_path):
    params, tool = prepare_case(tmp_path)
    (tmp_path / "stdout.out").write_text(synthetic_output(tmp_path / "geometry.xyz"))
    (tmp_path / "job.property.json").write_text("{broken")
    before = snapshot(tmp_path)
    result = read_outputs(tmp_path, params, tool)
    assert not result["qualified_outputs"]
    assert result["diagnostics"][0]["category"] == "parse_conflict_or_invalid_json"
    assert snapshot(tmp_path) == before


def test_json_energy_conflict_never_publishes(tmp_path):
    params, tool = prepare_case(tmp_path)
    (tmp_path / "stdout.out").write_text(synthetic_output(tmp_path / "geometry.xyz"))
    data = {"Calculation_Info": {"Charge": 0, "Mult": 1},
            "Calculation_Status": {"Version": "6.1.1"},
            "Geometries": [{"Single_Point_Data": {"FinalEnergy": -74.0, "Converged": True}}]}
    (tmp_path / "job.property.json").write_text(json.dumps(data))
    before = snapshot(tmp_path)
    result = read_outputs(tmp_path, params, tool)
    assert not result["qualified_outputs"]
    assert not result["observations"]["parser_consistent"]
    assert snapshot(tmp_path) == before


def test_frozen_cases_have_valid_geometry_and_no_reference_fabrication():
    from orca_agent.tools.registry import validate_geometry
    cases = json.loads((FIXTURES / "cases.json").read_text())["cases"]
    for case in cases:
        atoms = validate_geometry((FIXTURES / case["geometry"]).read_text(),
                                  CalculationParameters(**case["parameters"]))
        assert len(atoms) in (3, 5)
        assert case["independent_reference"]["energy_eh"] is None
        assert case["scientific_status"] == "not_verified"


def test_real_water_sp_format_and_read_only_regression():
    directory = FIXTURES / "real_water_sp"
    provenance = json.loads((directory / "provenance.json").read_text())
    before = snapshot(directory)
    for name, expected in provenance["files_sha256"].items():
        assert before[name] == expected
    manifest = json.loads((directory / "input-manifest.json").read_text())
    result = read_outputs(directory, manifest["parameters"], manifest["tool"])
    assert result["observations"]["conditions_match"]
    assert result["observations"]["scf_converged"]
    assert result["observations"]["parser_consistent"]
    assert "energy" in result["qualified_outputs"]
    line = result["observations"]["evidence"]["scf_converged"]["line"]
    assert b"SCF CONVERGED AFTER" in (directory / "stdout.out").read_bytes().split(b"\n")[line - 1]
    assert snapshot(directory) == before


def test_real_orca_alternative_convergence_does_not_lower_frozen_criteria():
    directory = FIXTURES / "real_water_opt_early_stop"
    before = snapshot(directory)
    for name, expected in json.loads((directory / "provenance.json").read_text())["files_sha256"].items():
        assert before[name] == expected
    manifest = json.loads((directory / "input-manifest.json").read_text())
    result = read_outputs(directory, manifest["parameters"], manifest["tool"])
    assert result["observations"]["optimization_converged"]
    assert not result["observations"]["optimization_thresholds_passed"]
    assert "energy" in result["qualified_outputs"]
    assert "optimized_geometry" not in result["qualified_outputs"]
    assert snapshot(directory) == before


def test_real_scf_iteration_limit_preserves_failure_without_qualified_energy():
    directory = FIXTURES / "real_water_scf_limit"
    before = snapshot(directory)
    provenance = json.loads((directory / "provenance.json").read_text())
    for name, expected in provenance["files_sha256"].items():
        assert before[name] == expected
    assert set(provenance["requested_files_absent_in_original"]) == {
        "job.property.json", "job.xyz"
    }
    manifest = json.loads((directory / "input-manifest.json").read_text())
    result = read_outputs(directory, manifest["parameters"], manifest["tool"])
    statuses = {check.name: check.status for check in result["checks"]["energy"]}
    assert statuses["scf_converged"] == "failed"
    assert statuses["finite_total_energy"] == "failed"
    assert not result["qualified_outputs"]
    assert "scf_not_converged" in {item["category"] for item in result["diagnostics"]}
    assert not (directory / "job.property.json").exists()
    assert snapshot(directory) == before


def test_real_opt_iteration_limit_retains_energy_without_qualified_structure():
    directory = FIXTURES / "real_water_opt_limit"
    before = snapshot(directory)
    provenance = json.loads((directory / "provenance.json").read_text())
    for name, expected in provenance["files_sha256"].items():
        assert before[name] == expected
    manifest = json.loads((directory / "input-manifest.json").read_text())
    result = read_outputs(directory, manifest["parameters"], manifest["tool"])
    statuses = {check.name: check.status for check in result["checks"]["optimized_geometry"]}
    assert statuses["optimization_converged"] == "failed"
    assert statuses["optimization_thresholds"] == "failed"
    assert all(check.status == "passed" for check in result["checks"]["energy"])
    assert set(result["qualified_outputs"]) == {"energy"}
    assert result["observations"]["energy_geometry"]["atoms"]
    assert "optimization_not_converged" in {item["category"] for item in result["diagnostics"]}
    assert (directory / "job.xyz").exists()
    assert snapshot(directory) == before


@pytest.mark.parametrize("json_mode", ["consistent", "missing", "conflict"])
@pytest.mark.parametrize("final_scf,expected", [("missing", "unverified"), ("failed", "failed")])
def test_final_opt_energy_cannot_borrow_prior_scf(tmp_path, json_mode, final_scf, expected):
    """Fault injection into a real multicyle output; this never runs ORCA."""
    source = FIXTURES / "real_water_opt_early_stop"
    original = snapshot(source)
    shutil.copytree(source, tmp_path, dirs_exist_ok=True)
    output = tmp_path / "stdout.out"
    lines = output.read_bytes().decode().split("\n")
    markers = [i for i, line in enumerate(lines) if re.match(
        r"\s*\**\s*SCF CONVERGED AFTER\s+\d+\s+CYCLES", line
    )]
    assert len(markers) > 1
    lines[markers[-1]] = "" if final_scf == "missing" else "SCF NOT CONVERGED AFTER 2 CYCLES"
    output.write_bytes("\n".join(lines).encode())
    prop = tmp_path / "job.property.json"
    if json_mode == "missing":
        prop.unlink()
    else:
        payload = json.loads(prop.read_text())
        final_data = payload["Geometries"][-1]["Single_Point_Data"]
        final_data["Converged"] = final_scf != "failed"
        if json_mode == "conflict":
            final_data["FinalEnergy"] += 0.5
        prop.write_text(json.dumps(payload))
    before = snapshot(tmp_path)
    manifest = json.loads((tmp_path / "input-manifest.json").read_text())
    result = read_outputs(tmp_path, manifest["parameters"], manifest["tool"])
    assert result["qualified_outputs"] == {}
    check = next(c for c in result["checks"]["energy"] if c.name == "scf_converged")
    assert check.status == expected
    evidence = check.source
    assert evidence["geometry_line"] > markers[-2] + 1
    assert evidence["segment_start_line"] <= evidence["geometry_line"]
    assert evidence["geometry_line"] < evidence["energy_line"] <= evidence["segment_end_line"]
    assert evidence["converged_lines"] == []
    categories = {item["category"] for item in result["diagnostics"]}
    if final_scf == "missing":
        assert "scf_convergence_unverified" in categories
        assert "scf_not_converged" not in categories
    else:
        assert "scf_not_converged" in categories
        assert evidence["failure_lines"] == [markers[-1] + 1]
    assert result["observations"]["parser_consistent"] == (json_mode != "conflict")
    assert snapshot(tmp_path) == before
    assert snapshot(source) == original


def test_opt_partial_energy_has_same_fragment_sources():
    directory = FIXTURES / "real_water_opt_limit"
    manifest = json.loads((directory / "input-manifest.json").read_text())
    result = read_outputs(directory, manifest["parameters"], manifest["tool"])
    assert set(result["qualified_outputs"]) == {"energy"}
    evidence = result["observations"]["evidence"]
    binding = evidence["energy_geometry_binding"]
    assert binding["geometry_line"] == evidence["scf_converged"]["geometry_line"]
    assert binding["energy_line"] == evidence["finite_total_energy"]["line"]
    assert binding["geometry_line"] < binding["converged_lines"][0] < binding["energy_line"]
    assert binding["geometry"]


def test_version_check_binds_frozen_environment(tmp_path):
    params, tool = prepare_case(tmp_path)
    (tmp_path / "stdout.out").write_text(synthetic_output(tmp_path / "geometry.xyz"))
    result = read_outputs(tmp_path, params, tool, expected_orca_version="6.1.2")
    assert not result["qualified_outputs"]
    assert result["observations"]["version_supported"] is False
    assert result["observations"]["evidence"]["orca_version"]["expected"] == "6.1.2"


def test_new_scientific_checks_use_current_explicit_version(tmp_path):
    params, tool = prepare_case(tmp_path)
    (tmp_path / "stdout.out").write_text(synthetic_output(tmp_path / "geometry.xyz"))
    result = read_outputs(tmp_path, params, tool)
    assert {c.rule_version for checks in result["checks"].values() for c in checks} == {"orca-hf-2"}


@pytest.mark.parametrize("damage", ["missing_geometry", "malformed_geometry", "invalid_energy"])
def test_current_segment_damage_does_not_select_previous_geometry_or_energy(tmp_path, damage):
    params, tool = prepare_case(tmp_path, "water_opt")
    text = synthetic_output(tmp_path / "geometry.xyz", normal=False)
    if damage == "missing_geometry":
        text += "GEOMETRY OPTIMIZATION CYCLE 2\n"
    elif damage == "malformed_geometry":
        text += "CARTESIAN COORDINATES (ANGSTROEM)\n--\ninvalid atoms\n"
    else:
        geometry_section = synthetic_output(tmp_path / "geometry.xyz", normal=False).split(
            "CARTESIAN COORDINATES (ANGSTROEM)", 1
        )[1]
        text += ("CARTESIAN COORDINATES (ANGSTROEM)" + geometry_section).replace(
            "FINAL SINGLE POINT ENERGY -75.123456789000", "FINAL SINGLE POINT ENERGY nan"
        )
    if damage != "invalid_energy":
        text += "SCF CONVERGED AFTER 3 CYCLES\nFINAL SINGLE POINT ENERGY -75.2\n"
    text += "ORCA TERMINATED NORMALLY\n"
    (tmp_path / "stdout.out").write_text(text)
    result = read_outputs(tmp_path, params, tool)
    assert not result["qualified_outputs"]
    if damage == "invalid_energy":
        assert result["observations"]["energy_eh"] is None
    else:
        assert result["observations"]["energy_geometry"] is None


def test_multiple_energy_candidates_in_one_segment_are_ambiguous(tmp_path):
    params, tool = prepare_case(tmp_path)
    text = synthetic_output(tmp_path / "geometry.xyz").replace(
        "ORCA TERMINATED NORMALLY",
        "FINAL SINGLE POINT ENERGY -75.2\nORCA TERMINATED NORMALLY",
    )
    (tmp_path / "stdout.out").write_text(text)
    result = read_outputs(tmp_path, params, tool)
    assert not result["qualified_outputs"]
    assert result["observations"]["scf_converged"] is None
    assert result["observations"]["parser_consistent"] is False


@pytest.mark.parametrize("tail_scf,expected", [
    ("SCF NOT CONVERGED AFTER 2 CYCLES", "failed"), ("", "unverified"),
])
def test_final_opt_cycle_without_energy_cannot_publish_previous_cycle(tmp_path, tail_scf, expected):
    params, tool = prepare_case(tmp_path, "water_opt")
    first = synthetic_output(tmp_path / "geometry.xyz", normal=False)
    coords = "\n".join((tmp_path / "geometry.xyz").read_text().splitlines()[2:])
    second = (
        "GEOMETRY OPTIMIZATION CYCLE 2\n"
        "CARTESIAN COORDINATES (ANGSTROEM)\n--\n" + coords
        + "\n\n" + tail_scf + "\nORCA TERMINATED NORMALLY\n"
    )
    (tmp_path / "stdout.out").write_text(first + second)
    result = read_outputs(tmp_path, params, tool)
    assert result["qualified_outputs"] == {}
    assert result["observations"]["energy_eh"] is None
    assert result["observations"]["previous_energy_observation"]["value"] == -75.123456789
    check = next(c for c in result["checks"]["energy"] if c.name == "scf_converged")
    assert check.status == expected
    assert check.source["energy_line"] is None
    assert check.source["geometry_line"] > first.count("\n")


def test_trailing_coordinate_report_alone_does_not_create_a_calculation(tmp_path):
    params, tool = prepare_case(tmp_path, "water_opt")
    text = synthetic_output(tmp_path / "geometry.xyz", normal=False)
    coords = "\n".join((tmp_path / "geometry.xyz").read_text().splitlines()[2:])
    text += "CARTESIAN COORDINATES (ANGSTROEM)\n--\n" + coords
    text += "\n\nORCA TERMINATED NORMALLY\n"
    (tmp_path / "stdout.out").write_text(text)
    result = read_outputs(tmp_path, params, tool)
    assert set(result["qualified_outputs"]) == {"energy"}


@pytest.mark.parametrize("version,observed,status", [
    ("6.1.1 RELEASE", "6.1.1", "passed"),
    ("6.1.0", "6.1.0", "failed"),
    ("6.1.2", "6.1.2", "failed"),
    ("6.2.0", "6.2.0", "failed"),
    ("6.1.1-f.1", "6.1.1-f.1", "failed"),
    ("6.1.1\nProgram Version 6.1.1", None, "unverified"),
    ("", None, "unverified"),
])
def test_output_version_tokens_are_exact_and_unique(tmp_path, version, observed, status):
    params, tool = prepare_case(tmp_path)
    text = synthetic_output(tmp_path / "geometry.xyz").replace(
        "Program Version 6.1.1", "Program Version " + version
    )
    (tmp_path / "stdout.out").write_text(text)
    result = read_outputs(tmp_path, params, tool)
    assert result["observations"]["orca_version"] == observed
    check = next(c for c in result["checks"]["energy"] if c.name == "orca_version")
    assert check.status == status
    assert bool(result["qualified_outputs"]) == (status == "passed")
