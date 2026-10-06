"""Parser regression: marked synthetic specimens and a traced real output subset."""

import hashlib
import json
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
