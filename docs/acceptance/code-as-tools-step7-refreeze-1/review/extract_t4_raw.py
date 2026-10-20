"""Review-only extraction from archived stdout; no task or scientific execution."""
from pathlib import Path
import datetime
import hashlib
import json
import re


def source(path):
    data = path.read_bytes()
    return {"path": str(path), "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}


review = Path(__file__).resolve().parent
workspace = review.parents[3]
start_path = workspace / "data/code-tools-refreeze-1-batch/first-repetition-evidence/T4/start.json"
start = json.loads(start_path.read_text(encoding="utf-8"))
data_root = Path(start["data_root"])
run_root = data_root / "runs" / start["run_id"]
result_path = run_root / "results/result_33ad80efc2aa415596e6944f71685f90.json"
result = json.loads(result_path.read_text(encoding="utf-8"))
raw_sources = {}
for name in ("task/stdout.out", "task/job.inp", "task/coords.xyz"):
    entry = result["source"]["files"][name]
    metadata_path = data_root / "artifacts" / entry["artifact_id"] / "artifact.json"
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    path = data_root / metadata["path"]
    info = source(path)
    info["artifact_id"] = entry["artifact_id"]
    info["hash_matches_result"] = info["sha256"] == entry["sha256"]
    info["hash_matches_artifact"] = info["sha256"] == metadata["sha256"]
    raw_sources[name] = info

stdout_path = Path(raw_sources["task/stdout.out"]["path"])
lines = stdout_path.read_text(encoding="utf-8").splitlines()
length_start = next(i for i, line in enumerate(lines) if "ABSORPTION SPECTRUM VIA TRANSITION ELECTRIC DIPOLE MOMENTS" in line)
velocity_start = next(i for i, line in enumerate(lines[length_start + 1:], length_start + 1) if "ABSORPTION SPECTRUM VIA TRANSITION VELOCITY DIPOLE MOMENTS" in line)
pattern = re.compile(
    r"^\s*0-1A\s*->\s*(\d+)-1A\s+"
    r"([-+\d.]+)\s+([-+\d.]+)\s+([-+\d.]+)\s+([-+\d.eE]+)\s+"
)
rows = []
for index in range(length_start, velocity_start):
    match = pattern.match(lines[index])
    if match:
        rows.append({
            "state": int(match[1]),
            "excitation_energy_eV": float(match[2]),
            "wavenumber_cm_inverse": float(match[3]),
            "wavelength_nm": float(match[4]),
            "oscillator_strength_length_dimensionless": float(match[5]),
            "line": index + 1,
            "raw_line": lines[index],
        })
markers = {}
for label, text in {
    "scf_convergence": "SCF CONVERGED AFTER",
    "full_tddft": "Tamm-Dancoff approximation",
    "triplets": "Generation of triplets",
    "response_energy_convergence": "*** CONVERGENCE OF ENERGIES REACHED ***",
    "response_done": "*** DAVIDSON DONE ***",
    "singlets": "TD-DFT EXCITED STATES (SINGLETS)",
    "normal_termination": "ORCA TERMINATED NORMALLY",
}.items():
    markers[label] = [{"line": i + 1, "raw_line": line} for i, line in enumerate(lines) if text in line]

payload = {
    "observed_at_utc": datetime.datetime.now(datetime.timezone.utc).isoformat(),
    "scope": "Independent review extraction from original archived stdout after prior input registration; no product-generated parser was executed.",
    "task": "T4",
    "run_id": start["run_id"],
    "result_id": result["id"],
    "input_manifest": [source(start_path), source(result_path)] + list(raw_sources.values()),
    "reviewer_code": source(Path(__file__).resolve()),
    "rows": rows,
    "raw_markers": markers,
    "checks": {
        "states_exactly_1_to_5": [row["state"] for row in rows] == [1, 2, 3, 4, 5],
        "length_gauge_header_identified": True,
        "weak_or_zero_entries_filtered_out": False,
        "raw_source_hashes_match": all(item["hash_matches_result"] and item["hash_matches_artifact"] for item in raw_sources.values()),
    },
    "limitations": [
        "Raw convergence and five-row consistency are bounded source checks, not independent numerical accuracy verification.",
        "No independent reference/control ORCA calculation was authorized or executed.",
        "The product Result retains scientific_validation=unverified and no scientific QualifiedOutput; this review does not mutate that status.",
        "All five printed oscillator strengths are positive in this output; weak states are retained.",
    ],
}
output = review / "t4-raw-response-check.json"
with output.open("x", encoding="utf-8", newline="\n") as f:
    json.dump(payload, f, ensure_ascii=False, indent=2)
    f.write("\n")
print(json.dumps({"output": source(output), "rows": rows, "checks": payload["checks"]}, ensure_ascii=False))
