"""The one OPI input/read adapter. No execution or implicit JSON conversion.

Property JSON, when present and valid, supplies the primary numeric energy.
The ORCA text is always required for convergence, input echo and stage binding;
missing JSON uses the explicitly supported text reader. Broken or contradictory
JSON fails checks, rather than silently falling back to a second execution path.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import shutil
from pathlib import Path
from typing import Any

from opi.core import Calculator
from opi.input.blocks import BlockGeom, BlockOutput, BlockScf
from opi.input.simple_keywords import SimpleKeyword
from opi.input.structures.structure_file import XyzFile
from opi.output.core import Output
from opi.utils.orca_version import OrcaVersion

from orca_agent.models import CalculationParameters
from orca_agent.orca.checks import check_outputs
from orca_agent.orca.diagnostics import diagnostic, scientific_diagnostics
from orca_agent.tools.registry import validate_geometry, validate_parameters
from orca_agent.versions import (
    extract_orca_version,
    is_supported_orca_version,
    orca_version_tokens,
)

MAX_EVIDENCE_BYTES = 10 * 1024 * 1024
NUMBER = r"[-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[EeDd][-+]?\d+)?"


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _read(path: Path) -> str:
    if path.is_symlink() or path.stat().st_size > MAX_EVIDENCE_BYTES:
        raise ValueError("Evidence is linked or exceeds the bounded reader limit")
    # Preserve CR/CRLF bytes so physical line numbers remain tied to LF boundaries.
    return path.read_bytes().decode("utf-8", errors="replace")


def _parameters(value: CalculationParameters | dict, tool_name: str) -> CalculationParameters:
    return validate_parameters(tool_name, value)


def prepare_input(
    workdir: str | Path,
    geometry_path: str | Path,
    parameters: CalculationParameters | dict,
    tool_name: str,
) -> dict[str, Any]:
    """Write fresh OPI input and an immutable input fingerprint manifest."""
    params = _parameters(parameters, tool_name)
    directory = Path(workdir).resolve()
    directory.mkdir(parents=True, exist_ok=True)
    source = Path(geometry_path).resolve(strict=True)
    geometry_text = _read(source)
    validate_geometry(geometry_text, params)
    geometry = directory / "geometry.xyz"
    inp = directory / "job.inp"
    manifest_path = directory / "input-manifest.json"
    if inp.exists() or manifest_path.exists():
        raise FileExistsError("An attempt's input may not be overwritten")
    if source != geometry:
        if geometry.exists():
            raise FileExistsError("An attempt's initial geometry may not be overwritten")
        shutil.copyfile(source, geometry)
    calculator = Calculator("job", working_dir=directory, version_check=False)
    calculator.structure = XyzFile(geometry, charge=params.charge, multiplicity=params.multiplicity)
    keywords = ["RHF", "STO-3G", "TightSCF", "NORI", "NoAutoStart"]
    if tool_name == "orca.opt":
        keywords.append("TightOpt")
    calculator.input.add_simple_keywords(*(SimpleKeyword(word) for word in keywords))
    calculator.input.ncores = params.cores
    calculator.input.memory = params.maxcore_mb
    scf = BlockScf(maxiter=params.scf_maxiter)
    # OPI 2.0 has no typed ConvForced field; this constant is adapter-owned.
    scf.add_option("ConvForced", "1")
    calculator.input.add_blocks(scf)
    if tool_name == "orca.opt":
        calculator.input.add_blocks(BlockGeom(
            maxiter=params.opt_maxiter, enforcestrictconvergence=True,
        ))
    # ORCA writes property JSON as part of this registered calculation. No converter.
    calculator.json_via_input = False
    calculator.input.add_blocks(BlockOutput(jsonpropfile=True, jsongbwfile=False))
    calculator.write_input(force=False)
    manifest = {
        "tool": tool_name, "parameters": params.model_dump(mode="json"),
        "geometry_sha256": _sha(geometry), "input_sha256": _sha(inp),
        "input_file": "job.inp", "geometry_file": "geometry.xyz",
    }
    with manifest_path.open("x", encoding="utf-8") as stream:
        json.dump(manifest, stream, indent=2, allow_nan=False)
        stream.write("\n")
    return {**manifest, "input_path": str(inp), "geometry_path": str(geometry)}


def _coordinates(lines: list[str]) -> list[dict[str, Any]]:
    blocks = []
    pattern = re.compile(rf"^\s*([A-Z][a-z]?)\s+({NUMBER})\s+({NUMBER})\s+({NUMBER})\s*$")
    for index, line in enumerate(lines):
        if line.strip() != "CARTESIAN COORDINATES (ANGSTROEM)":
            continue
        atoms = []
        for row in lines[index + 1:]:
            match = pattern.match(row)
            if match:
                atoms.append((match[1], *(float(x.replace("D", "E")) for x in match.groups()[1:])))
            elif atoms:
                break
            elif row.strip() and not set(row.strip()) <= {"-"}:
                break
        if atoms:
            blocks.append({"line": index + 1, "atoms": atoms})
    return blocks


def _same_geometry(first: list, second: list, tolerance: float = 3e-6) -> bool:
    if len(first) != len(second) or [x[0] for x in first] != [x[0] for x in second]:
        return False
    # Atom-indexed distances allow ORCA translations/rotations, never permutations.
    for i in range(len(first)):
        for j in range(i):
            a = math.dist(first[i][1:], first[j][1:])
            b = math.dist(second[i][1:], second[j][1:])
            if not math.isfinite(a + b) or abs(a - b) > tolerance:
                return False
    return True


def _thresholds(lines: list[str], converged_line: int) -> tuple[bool, dict]:
    """Require the final TightOpt table, its actual thresholds and all five values."""
    required = {
        "Energy change": 1e-6, "RMS gradient": 3e-5, "MAX gradient": 1e-4,
        "RMS step": 6e-4, "MAX step": 1e-3,
    }
    found = {}
    headers = [index for index, line in enumerate(lines[:converged_line])
               if "geometry convergence" in line.lower()]
    if not headers:
        return False, found
    start = headers[-1]
    for line_no, line in enumerate(lines[start:converged_line], start + 1):
        for label, expected in required.items():
            match = re.search(rf"{label}\s+({NUMBER})\s+({NUMBER})\s+(YES|NO)", line, re.I)
            if match:
                value, threshold = float(match[1]), float(match[2])
                found[label] = {
                    "line": line_no, "value": value, "threshold": threshold,
                    "passed": match[3].upper() == "YES" and abs(value) <= expected * 1.00001
                    and abs(threshold - expected) <= expected * 1e-5,
                }
    return len(found) == len(required) and all(x["passed"] for x in found.values()), found


def _energy_fragment(lines: list[str], blocks: list[dict], energies: list[dict],
                     converged: list[int], failures: list[int]) -> dict:
    """Bind one final energy to one coordinate/SCF segment of the admitted format.

    A new coordinate block or an explicit optimization stage ends a segment.
    The latter also prevents a damaged stage with no coordinates from borrowing
    the previous stage's geometry. Multiple energies or SCF success markers in
    one segment are ambiguous; the supported RHF profile needs neither.
    """
    stages = [i for i, line in enumerate(lines, 1) if re.match(
        r"\s*\**\s*(?:GEOMETRY OPTIMIZATION CYCLE\s+\d+\b|"
        r"FINAL ENERGY EVALUATION AT THE STATIONARY POINT\b)", line
    )]
    coordinate_headers = [i for i, line in enumerate(lines, 1)
                          if line.strip() == "CARTESIAN COORDINATES (ANGSTROEM)"]
    boundaries = sorted(set(stages + coordinate_headers))
    energy = energies[-1] if energies else None
    # A later actual calculation may have stopped before printing any energy.
    # A trailing coordinate report alone is not evidence of a new calculation.
    later_stages = [line for line in stages if energy and line > energy["line"]]
    later_scf = [line for line in converged + failures if energy and line > energy["line"]]
    previous_energy = energy if later_stages or later_scf else None
    if previous_energy:
        energy = None
    if energy:
        target = energy["line"]
    else:
        last_stage = stages[-1] if stages else 0
        current_scf = [line for line in converged + failures if line >= last_stage]
        target = max(current_scf) if current_scf else len(lines)
    prior_boundaries = [line for line in boundaries if line <= target]
    start = prior_boundaries[-1] if prior_boundaries else 1
    if previous_energy:
        start = max(start, previous_energy["line"] + 1)
    end = next((line - 1 for line in boundaries if line > start), len(lines))
    geometry = next((block for block in blocks if block["line"] == start), None)
    local_energies = [item for item in energies if start <= item["line"] <= end]
    successes = [line for line in converged if start <= line <= end]
    failed = [line for line in failures if start <= line <= end]
    unique = bool(energy and geometry and len(local_energies) == 1)
    conflict = len(successes) > 1 or bool(successes and failed) or len(local_energies) > 1
    scf = None
    reason = "No SCF convergence marker in the energy's coordinate segment."
    if conflict:
        reason = "Conflicting or multiple SCF/energy candidates in one coordinate segment."
    elif failed:
        scf = False
        reason = "SCF explicitly failed in this coordinate segment."
    elif successes and unique and successes[0] < energy["line"]:
        scf = True
        reason = "One converged SCF precedes the energy in the same coordinate segment."
    elif not unique:
        reason = "No unique coordinate/energy segment can be established."
    return {
        "file": "stdout.out", "segment_start_line": start, "segment_end_line": end,
        "geometry_line": geometry["line"] if geometry else None,
        "energy_line": energy["line"] if energy else None,
        "converged_lines": successes, "failure_lines": failed,
        "stage_line": next((line for line in reversed(stages) if line <= start), None),
        "unique_binding": unique, "conflict": conflict, "scf_converged": scf,
        "detail": reason, "geometry": geometry, "energy": energy,
        "previous_energy_observation": {
            **previous_energy, "file": "stdout.out", "unit": "Eh",
            "qualification": "observation_only; not the current calculation fragment",
        } if previous_energy else None,
    }


def _text_observations(text: str, params: CalculationParameters, tool_name: str) -> dict:
    lines = [line.rstrip("\r") for line in text.split("\n")]
    energies = []
    for line_no, line in enumerate(lines, 1):
        label = re.match(r"\s*FINAL SINGLE POINT ENERGY\b(.*)$", line)
        if label:
            match = re.fullmatch(rf"\s*({NUMBER})\s*", label[1])
            energies.append({
                "line": line_no,
                "value": float(match[1].replace("D", "E")) if match else None,
            })
    converged = [i for i, line in enumerate(lines, 1) if re.match(
        r"\s*\**\s*SCF CONVERGED AFTER\s+\d+\s+CYCLES", line
    )]
    failures = [i for i, line in enumerate(lines, 1) if re.match(
        r"\s*\**\s*(?:SCF (?:NOT CONVERGED|DID NOT CONVERGE)|SCF CONVERGENCE FAILURE)",
        line, re.I
    )]
    opt = [i for i, line in enumerate(lines, 1) if re.match(
        r"\s*\**\s*THE OPTIMIZATION HAS CONVERGED\s*\**\s*$", line
    )]
    normal = [i for i, line in enumerate(lines, 1) if re.match(
        r"\s*\**\s*ORCA TERMINATED NORMALLY\s*\**\s*$", line
    )]
    blocks = _coordinates(lines)
    fragment = _energy_fragment(lines, blocks, energies, converged, failures)
    last = fragment["energy"]
    fragment_source = {key: value for key, value in fragment.items()
                       if key not in {"geometry", "energy", "previous_energy_observation"}}
    version = extract_orca_version(text)
    # Echo alone is insufficient: also require runtime RHF and electronic state.
    hftypes = re.findall(r"^\s*Hartree-Fock type\s+HFTyp\s*\.{2,}\s*(\S+)", text, re.M | re.I)
    charges = re.findall(r"^\s*Total Charge\s+Charge\s*\.{2,}\s*(-?\d+)", text, re.M)
    mults = re.findall(r"^\s*Multiplicity\s+Mult\s*\.{2,}\s*(\d+)", text, re.M)
    bases = re.findall(r"^\s*Your calculation utilizes the basis\s*:\s*(\S+)", text, re.M | re.I)
    thresholds, threshold_evidence = _thresholds(lines, opt[-1] if opt else 0)
    return {
        "energy_eh": last["value"] if last else None,
        "energy_unit": "Eh", "energy_source": "stdout.out",
        "normal_termination": bool(normal and (not last or normal[-1] > last["line"])),
        "orca_version": version,
        "version_supported": is_supported_orca_version(version) if version else None,
        "conditions_match": bool(hftypes and bases and charges and mults
                                 and all(value.upper() == "RHF" for value in hftypes)
                                 and all(value.upper() == "STO-3G" for value in bases)
                                 and all(int(value) == params.charge for value in charges)
                                 and all(int(value) == params.multiplicity for value in mults)),
        "scf_converged": fragment["scf_converged"],
        "parser_consistent": not fragment["conflict"],
        "optimization_converged": bool(opt),
        "optimization_thresholds_passed": thresholds,
        "geometry_blocks": blocks,
        "energy_geometry": fragment["geometry"],
        "energy_fragment": fragment_source,
        "previous_energy_observation": fragment["previous_energy_observation"],
        "evidence": {
            "orca_version": {
                "file": "stdout.out", "tokens": list(orca_version_tokens(text)),
                "lines": [i for i, row in enumerate(lines, 1)
                          if re.match(r"\s*Program Version\s+", row)],
            },
            "method_and_electronic_state": {"file": "stdout.out", "lines": [
                i for i, row in enumerate(lines, 1) if re.match(
                    r"\s*(?:Hartree-Fock type\s+HFTyp|Total Charge\s+Charge|"
                    r"Multiplicity\s+Mult|Your calculation utilizes the basis\s*:)", row
                )
            ]},
            "finite_total_energy": {**fragment_source, **(last or {})},
            "scf_converged": {
                **fragment_source,
                "line": fragment["converged_lines"][0] if fragment["scf_converged"] is True
                else (fragment["failure_lines"][0] if fragment["failure_lines"] else None),
            },
            "normal_termination": {"file": "stdout.out", "line": normal[-1] if normal else None},
            "optimization_converged": {"file": "stdout.out", "line": opt[-1] if opt else None},
            "optimization_thresholds": {"file": "stdout.out", "rows": threshold_evidence},
        },
    }


def read_outputs(
    workdir: str | Path, parameters: CalculationParameters | dict, tool_name: str,
    *, expected_orca_version: str | None = None,
) -> dict[str, Any]:
    """Read existing bounded evidence, then check every proposed scientific port."""
    params = _parameters(parameters, tool_name)
    directory = Path(workdir).resolve(strict=True)
    diagnostics = []
    facts: dict[str, Any] = {"parser_consistent": True, "evidence": {}}
    try:
        facts.update(_text_observations(_read(directory / "stdout.out"), params, tool_name))
    except (OSError, ValueError) as error:
        diagnostics.append(diagnostic("missing_or_invalid_output", str(error)))
    if expected_orca_version is not None:
        facts["version_supported"] = bool(
            facts.get("version_supported")
            and facts.get("orca_version") == expected_orca_version
        )
    facts["evidence"].setdefault("orca_version", {}).update({
        "observed": facts.get("orca_version"), "expected": expected_orca_version,
    })
    try:
        manifest = json.loads(_read(directory / "input-manifest.json"))
        facts["input_integrity"] = (
            manifest["tool"] == tool_name and manifest["parameters"] == params.model_dump(mode="json")
            and manifest["input_sha256"] == _sha(directory / "job.inp")
            and manifest["geometry_sha256"] == _sha(directory / "geometry.xyz")
        )
        initial = validate_geometry(_read(directory / "geometry.xyz"), params)
        blocks = facts.get("geometry_blocks", [])
        facts["initial_geometry_matches"] = bool(blocks and _same_geometry(initial, blocks[0]["atoms"]))
        energy_geometry = facts.get("energy_geometry")
        facts["energy_geometry_bound"] = bool(
            facts.get("energy_fragment", {}).get("unique_binding") and energy_geometry
            and [a[0] for a in initial] == [a[0] for a in energy_geometry["atoms"]]
        )
        if tool_name == "orca.sp":
            facts["energy_geometry_bound"] = bool(
                facts["energy_geometry_bound"] and _same_geometry(initial, energy_geometry["atoms"])
            )
        if tool_name == "orca.opt" and (directory / "job.xyz").is_file():
            final = validate_geometry(_read(directory / "job.xyz"), params)
            facts["final_geometry_matches"] = bool(
                energy_geometry and _same_geometry(final, energy_geometry["atoms"])
            )
            facts["final_geometry_file"] = "job.xyz"
            facts["evidence"]["final_geometry"] = {
                "file": "job.xyz", "sha256": _sha(directory / "job.xyz"),
                "compared_with": "stdout.out", "line": energy_geometry["line"] if energy_geometry else None,
            }
        facts["evidence"]["input_integrity"] = {"file": "input-manifest.json"}
        facts["evidence"]["initial_geometry"] = {
            "file": "geometry.xyz", "sha256": _sha(directory / "geometry.xyz"),
            "compared_with": "stdout.out", "line": blocks[0]["line"] if blocks else None,
        }
        facts["evidence"]["energy_geometry_binding"] = {
            **facts.get("energy_fragment", {}),
            "file": "stdout.out", "line": energy_geometry["line"] if energy_geometry else None,
            "geometry": energy_geometry["atoms"] if energy_geometry else None,
        }
    except (OSError, ValueError, KeyError, TypeError) as error:
        facts["input_integrity"] = False
        diagnostics.append(diagnostic("input_or_geometry_invalid", str(error)))
    prop_path = directory / "job.property.json"
    if prop_path.is_file():
        try:
            _read(prop_path)  # Bound input before allowing OPI to consume it.
            parsed = Output("job", working_dir=directory, version_check=False, parse=False)
            parsed.do_redump_jsons = False
            parsed.parse(do_create_property_json=False, do_create_gbw_json=False,
                         read_prop_json=True, read_gbw_json=False)
            if str(OrcaVersion.from_json(parsed.property_json_data or {})) != facts.get("orca_version"):
                raise ValueError("Property JSON and text ORCA versions disagree")
            json_energy = parsed.get_final_energy()
            if json_energy is not None:
                text_energy = facts.get("energy_eh")
                if text_energy is None or abs(json_energy - text_energy) > 5e-8:
                    raise ValueError("Property JSON and raw text final energies disagree")
                facts["energy_eh"] = json_energy
                facts["energy_source"] = "job.property.json:geometries[-1].single_point_data.finalenergy"
                facts["evidence"]["finite_total_energy"]["json_field"] = facts["energy_source"]
            if parsed.get_charge() not in (None, params.charge):
                raise ValueError("JSON charge disagrees with the requested state")
            if parsed.get_mult() not in (None, params.multiplicity):
                raise ValueError("JSON multiplicity disagrees with the requested state")
            if parsed.results_properties and parsed.results_properties.geometries:
                data = parsed.results_properties.geometries[-1].single_point_data
                if data:
                    facts["property_json_scf_converged"] = data.converged
                if (data and data.converged is not None
                        and facts.get("scf_converged") is not None
                        and data.converged != facts["scf_converged"]):
                    raise ValueError("JSON and text SCF convergence disagree")
            json_structure = parsed.get_structure(with_fragments=False)
            if json_structure is not None:
                json_atoms = validate_geometry(json_structure.to_xyz_block(), params)
                text_geometry = facts.get("energy_geometry")
                if not text_geometry or not _same_geometry(json_atoms, text_geometry["atoms"]):
                    raise ValueError("JSON final geometry and text energy geometry disagree")
            facts["property_json_status"] = "read_existing_without_conversion"
        except (OSError, ValueError, TypeError, KeyError, AttributeError, RuntimeError) as error:
            facts["parser_consistent"] = False
            diagnostics.append(diagnostic("parse_conflict_or_invalid_json", str(error)))
    else:
        facts["property_json_status"] = "missing; explicit text-reading capability used"
    facts["evidence"]["parser_consistency"] = {
        "primary_source": facts.get("energy_source"),
        "property_json_status": facts.get("property_json_status", "invalid_or_conflicting"),
        "text_source": "stdout.out", "postprocess_executed": False,
    }
    checks = check_outputs(facts, tool_name)
    qualified = {}
    if all(check.status == "passed" for check in checks["energy"]):
        qualified["energy"] = {"value": facts["energy_eh"], "unit": "Eh"}
    if tool_name == "orca.opt" and all(
        check.status == "passed" for check in checks["optimized_geometry"]
    ):
        qualified["optimized_geometry"] = {"geometry_file": "job.xyz"}
    diagnostics.extend(scientific_diagnostics(facts, tool_name))
    return {"observations": facts, "checks": checks,
            "qualified_outputs": qualified, "diagnostics": diagnostics}
