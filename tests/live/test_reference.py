"""Explicit live reference comparisons using prewritten, separately authored inputs.

The only test override is input construction. Permissions, durable budgets, the
shared environment lease, managed process tree and artifact collection all use
the production runner. This module is never imported by the product CLI.
"""

from __future__ import annotations

import json
import math
import os
import re
import shutil
from pathlib import Path
from uuid import uuid4

import pytest

from orca_agent import runner
from orca_agent.config import Config
from orca_agent.store import Store, atomic_write, sha256_file
from orca_agent.tools import calculation

PROJECT = Path(__file__).resolve().parents[2]
FIXTURES = PROJECT / "tests" / "fixtures" / "phase_a"
ACCEPTANCE = PROJECT / "data" / "acceptance"
pytestmark = pytest.mark.live


def _read_json(path):
    return json.loads(path.read_text(encoding="utf-8"))


def _independent_energy(path):
    """A small independent raw-output check; never call the OPI/product reader."""
    text = path.read_bytes().decode("utf-8")
    matches = list(re.finditer(
        r"^\s*FINAL SINGLE POINT ENERGY\s+([-+]?\d+\.\d+(?:[Ee][-+]?\d+)?)\s*$",
        text, re.M,
    ))
    assert matches, "reference raw output has no finite total-energy field"
    last = matches[-1]
    energy = float(last[1])
    assert math.isfinite(energy)
    scf = list(re.finditer(r"^\s*\**\s*SCF CONVERGED AFTER\s+\d+\s+CYCLES", text, re.M))
    assert scf and scf[-1].start() < last.start()
    assert "ORCA TERMINATED NORMALLY" in text[last.end():]
    assert "SCF NOT CONVERGED" not in text[scf[-1].start():]
    assert re.search(r"Hartree-Fock type\s+HFTyp\s*\.+\s*RHF", text)
    assert "Your calculation utilizes the basis: STO-3G" in text
    mantissa, _, exponent = last[1].lower().partition("e")
    quantum = 10.0 ** (int(exponent or "0") - len(mantissa.partition(".")[2]))
    rounding_tolerance = quantum / 2 + 8 * math.ulp(energy)
    return energy, text[:last.start(1)].count("\n") + 1, rounding_tolerance


def _independent_distances(path):
    lines = path.read_text(encoding="utf-8").splitlines()
    number = int(lines[0])
    rows = [line.split() for line in lines[2:] if line.strip()]
    assert len(rows) == number
    elements = [row[0] for row in rows]
    points = [[float(value) for value in row[1:]] for row in rows]
    assert all(len(point) == 3 and all(math.isfinite(value) for value in point)
               for point in points)
    distances = [math.dist(points[i], points[j]) for i in range(number) for j in range(i)]
    return elements, distances


def _review_standard_input(case):
    """Static developer review, not a claim of a human chemistry expert's sign-off."""
    path = FIXTURES / case["manual_reference_input"]
    text = path.read_text(encoding="utf-8").upper()
    assert "! RHF STO-3G TIGHTSCF NORI NOAUTOSTART" in text
    assert "%PAL NPROCS 4 END" in text and "%MAXCORE 192" in text
    assert "CONVFORCED 1" in text and "* XYZFILE 0 1 GEOMETRY.XYZ" in text
    assert "MAXITER 100" in text
    assert ("TIGHTOPT" in text) == (case["tool"] == "orca.opt")
    if case["tool"] == "orca.opt":
        assert "ENFORCESTRICTCONVERGENCE TRUE" in text
    assert "JSON" not in text, "frozen reference input must not request a conversion"
    geometry = FIXTURES / case["geometry"]
    atoms, _ = _independent_distances(geometry)
    assert sum({"H": 1, "C": 6, "O": 8}[atom] for atom in atoms) == 10
    return {
        "review_kind": "developer_static_input_review_and_independent_raw_output_checks",
        "human_expert_approval": False,
        "reference_authorship": "project prewritten reference, based on official ORCA 6.1 manual",
        "independence": "reference input and raw comparison do not use product OPI construction/parser",
        "limitations": "same ORCA engine and backend; no cross-engine or experimental accuracy claim",
        "input_path": str(path), "input_sha256": sha256_file(path),
        "geometry_path": str(geometry), "geometry_sha256": sha256_file(geometry),
    }


@pytest.mark.parametrize("case_id", ["water_sp", "methane_sp", "water_opt", "methane_opt"])
def test_independent_frozen_reference(case_id, monkeypatch):
    production_receipt = ACCEPTANCE / f"{case_id}.json"
    if not production_receipt.exists():
        pytest.skip("production evidence missing; independent numerical reference is unverified")
    case = next(item for item in _read_json(FIXTURES / "cases.json")["cases"] if item["id"] == case_id)
    review = _review_standard_input(case)
    baseline = _read_json(production_receipt)
    production_store = Store(baseline["store_root"])
    production_run = production_store.load_run(baseline["run_id"])
    production_plan = production_store.load_plan(production_run)
    assert len(production_plan.steps) == 1
    assert production_plan.steps[0].tool == case["tool"]
    assert production_plan.steps[0].parameters.model_dump(mode="json") == case["parameters"]
    assert len(production_run.attempts) == 1
    production_attempt = production_run.attempts[0]
    product_result = production_store.load_result(production_run.id, production_attempt.result_id)
    assert "energy" in product_result.qualified_outputs
    if case["tool"] == "orca.opt":
        assert "optimized_geometry" in product_result.qualified_outputs
    product_workdir = production_store.path(production_attempt.directory)
    assert sha256_file(product_workdir / "geometry.xyz") == review["geometry_sha256"]
    production_energy, production_line, print_rounding = _independent_energy(product_workdir / "stdout.out")
    assert abs(product_result.qualified_outputs["energy"].value - production_energy) <= print_rounding
    receipt_path = ACCEPTANCE / f"reference-{case_id}.json"
    if receipt_path.exists():
        previous = _read_json(receipt_path)
        assert previous["production_run_id"] == production_run.id, "reference belongs to an older run"
        for entry in previous["evidence_files"]:
            assert sha256_file(Path(entry["path"])) == entry["sha256"]
        assert previous["passed"], "prior reference failed; preserved without automatic retry"
        return

    reference_root = PROJECT / "data" / "reference"
    reference_store = Store(reference_root)  # Deliberately use the shared default environment lease.
    spec_dir = reference_root / "specs" / f"{case_id}-{uuid4().hex}"
    spec_dir.mkdir(parents=True, exist_ok=False)
    shutil.copyfile(FIXTURES / case["geometry"], spec_dir / "geometry.xyz")
    goals = [{"name": "energy", "step": "reference", "port": "energy"}]
    if case["tool"] == "orca.opt":
        goals.append({"name": "geometry", "step": "reference", "port": "optimized_geometry"})
    spec = {
        "description": f"Independent frozen reference for {case_id}; test-only input override",
        "geometry": "geometry.xyz",
        "steps": [{"name": "reference", "tool": case["tool"], "parameters": case["parameters"]}],
        "goals": goals,
        "budget": {"attempts_per_step": 1, "orca_starts": 1, "extra_orca_starts": 0,
                   "run_seconds": case["parameters"]["timeout_seconds"] + 60},
    }
    spec_path = spec_dir / "task.json"
    atomic_write(spec_path, (json.dumps(spec, indent=2) + "\n").encode(), immutable=True)

    def frozen_input(workdir, geometry_path, parameters, tool_name):
        # Exact existing bytes are the independent oracle; no OPI or scientific generation.
        assert tool_name == case["tool"]
        assert parameters.model_dump(mode="json") == case["parameters"]
        assert sha256_file(Path(geometry_path)) == review["geometry_sha256"]
        directory = Path(workdir)
        inp, geometry = directory / "job.inp", directory / "geometry.xyz"
        assert not inp.exists() and not geometry.exists()
        shutil.copyfile(FIXTURES / case["manual_reference_input"], inp)
        shutil.copyfile(geometry_path, geometry)
        manifest = {
            "tool": tool_name, "parameters": parameters.model_dump(mode="json"),
            "input_sha256": sha256_file(inp), "geometry_sha256": sha256_file(geometry),
            "input_file": "job.inp", "geometry_file": "geometry.xyz",
        }
        atomic_write(directory / "input-manifest.json",
                     (json.dumps(manifest, indent=2) + "\n").encode(), immutable=True)
        return {**manifest, "input_path": str(inp), "geometry_path": str(geometry)}

    monkeypatch.setattr(calculation, "prepare_input", frozen_input)
    config = Config(
        orca_path=Path(os.environ.get("ORCA_AGENT_ORCA", "E:/orca/orca.exe")).resolve(),
        mpi_path=Path(os.environ.get("ORCA_AGENT_MPI", "C:/Program Files/Microsoft MPI/Bin/mpiexec.exe")).resolve(),
        data_root=reference_root,
    )
    run = runner.initialize(reference_store, config, spec_path)
    summary = {"case": case_id, "production_run_id": production_run.id,
               "store_root": str(reference_root), "run_id": run.id,
               "review": review, "passed": False, "evidence_files": []}
    try:
        reference_environment = _read_json(reference_store.path(f"runs/{run.id}/environment.json"))
        production_environment = _read_json(production_store.path(f"runs/{production_run.id}/environment.json"))
        assert reference_environment["orca"]["sha256"] == production_environment["orca"]["sha256"]
        run = runner.execute(reference_store, config, run.id)
        assert len(run.attempts) == 1
        attempt = run.attempts[0]
        directory = reference_store.path(attempt.directory)
        summary.update(result_ids=run.result_ids, usage=run.usage.model_dump(mode="json"),
                       run_state=run.state, execution=_read_json(directory / "execution.json"))
        assert run.usage.orca_starts_actual == 1 and run.usage.logical_attempts
        assert not (directory / "job.2jsonout").exists(), "reference unexpectedly invoked JSON conversion"
        for path in [directory / "job.inp", directory / "stdout.out", directory / "execution.json",
                     product_workdir / "stdout.out", Path(review["input_path"]), Path(review["geometry_path"])]:
            summary["evidence_files"].append({"path": str(path), "sha256": sha256_file(path)})
        reference_energy, reference_line, _ = _independent_energy(directory / "stdout.out")
        difference = abs(reference_energy - production_energy)
        summary["energy"] = {
            "reference_eh": reference_energy, "reference_stdout_line": reference_line,
            "production_eh": production_energy, "production_stdout_line": production_line,
            "difference_eh": difference, "tolerance_eh": 1e-7,
        }
        assert difference <= 1e-7
        assert summary["execution"]["state"] == "completed"
        assert run.state == "completed"
        assert reference_store.environment_lease() is None
        if case["tool"] == "orca.opt":
            reference_atoms, reference_distances = _independent_distances(directory / "job.xyz")
            product_atoms, product_distances = _independent_distances(product_workdir / "job.xyz")
            assert reference_atoms == product_atoms
            maximum = max(abs(a - b) for a, b in zip(reference_distances, product_distances, strict=True))
            summary["geometry"] = {"max_distance_difference_angstrom": maximum,
                                   "tolerance_angstrom": 1e-5}
            assert maximum <= 1e-5
            for path in [directory / "job.xyz", product_workdir / "job.xyz"]:
                summary["evidence_files"].append({"path": str(path), "sha256": sha256_file(path)})
        summary["passed"] = True
    finally:
        latest = reference_store.load_run(run.id)
        summary["usage"] = latest.usage.model_dump(mode="json")
        summary["run_state"] = latest.state
        atomic_write(receipt_path, (json.dumps(summary, indent=2) + "\n").encode(), immutable=True)
