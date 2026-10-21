"""Read-only H1 audit helpers; importing this file reads no candidate or old audit."""

import hashlib
import json
import re
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def read(path):
    return json.loads(path.read_text(encoding="utf-8"))


def sha(path):
    with path.open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


def reference(path):
    return {"path": str(path.relative_to(ROOT)), "sha256": sha(path), "bytes": path.stat().st_size}


def settled_candidate(candidate):
    """Require explicit candidate identity and original settled records before auditing."""
    assert re.fullmatch(r"a[0-9]{3}", candidate)
    base = ROOT / "data/live-repair/attempts" / candidate
    start, capture = read(base / "start.json"), read(base / "attempt.json")
    outcome = read(base / "outcome.json")  # No waiting or polling in an audit.
    assert start["candidate"] == capture["candidate"] == outcome["candidate"] == candidate
    assert start["task"] == capture["task"] == outcome["task"] == "H1"
    run_dir = base / "data/runs" / start["run_id"]
    run = read(run_dir / "run.json")
    assert run["id"] == start["run_id"] == outcome["run_id"]
    assert not any(item["state"] in {"intent", "running", "unknown"} for item in run["attempts"])
    originals = {path: sha(path) for path in base.rglob("*") if path.is_file()}
    return base, run_dir, run, outcome, originals


def original_task(base, request):
    authority = ROOT / "docs/acceptance/code-as-tools-step7-refreeze-4/freeze/holdout-selection.json"
    task = next(item for item in read(authority)["tasks"] if item["id"] == "H1")
    text = (base / "original.txt").read_text(encoding="utf-8")
    assert text.split("\n\n", 1)[0] == task["original_text"]
    assert text == request["original_text"] and "1701" in text.split("\n\n", 1)[1]
    return task, reference(authority)


def bound_source(data, run, result, filename):
    """Consume one exact Result file key, with no basename fallback or directory search."""
    assert result["run_id"] == run["id"]
    binding = result["source"]["files"][filename]
    metadata_path = data / "artifacts" / binding["artifact_id"] / "artifact.json"
    metadata = read(metadata_path)
    assert metadata["run_id"] == run["id"]
    assert metadata["attempt_id"] == result["attempt_id"]
    path = (data / metadata["path"]).resolve()
    assert path.is_relative_to(data.resolve())
    assert sha(path) == binding["sha256"] == metadata["sha256"]
    assert path.stat().st_size == metadata["size"]
    return path, {**reference(path), "artifact_id": binding["artifact_id"],
                  "artifact_metadata": reference(metadata_path), "same_run_attempt_and_hash": True}


def native_sources(base, directory, run):
    """Find direct and SDK native receipts without assuming a direct Result.

    Uses the shared same-Run/parent/receipt/Artifact/hash chain. Outer Python
    completion does not establish inner ORCA completion or actual startup.
    Each row's files map preserves both canonical and original archived keys.
    Property parsing and final Goal delivery remain separate audit steps.
    """
    from native_raw_sources import discover_native_sources

    assert directory.resolve() == (base / "data/runs" / run["id"]).resolve()
    return discover_native_sources(base / "data", run, reference)


def total_tensors(stdout):
    """Independently parse printed TOTAL tensors and printed Total iso, never a task CSV."""
    headers = list(re.finditer(r"Nucleus\s+(\d+)([A-Z][a-z]?)\s*:", stdout))
    summary = stdout.index("CHEMICAL SHIELDING SUMMARY")
    nuclei = []
    for index, header in enumerate(headers):
        end = headers[index + 1].start() if index + 1 < len(headers) else summary
        block = stdout[header.start():end]
        match = re.search(r"Total shielding tensor \(ppm\):\s*\n((?:[^\n]*\n){3})", block)
        assert match is not None, "Missing complete total tensor; never fill absent nuclei"
        tensor = [line.split() for line in match[1].strip().splitlines()]
        assert len(tensor) == 3 and all(len(row) == 3 for row in tensor)
        assert all(Decimal(value).is_finite() for row in tensor for value in row)
        iso_match = re.search(r"^\s*Total\s+[^\n]+iso=\s*([-+0-9.Ee]+)", block, re.M)
        assert iso_match is not None, "Missing independent printed total isotropic field"
        iso = iso_match[1]
        trace = sum(Decimal(tensor[i][i]) for i in range(3)) / 3
        delta = abs(trace - Decimal(iso))
        nuclei.append({"index_zero_based": int(header[1]), "element": header[2],
            "header_line": stdout[:header.start()].count("\n") + 1,
            "total_tensor_ppm": tensor, "printed_isotropic_ppm": iso,
            "trace_over_three_ppm": str(trace), "absolute_difference_ppm": str(delta),
            "consistent_within_stdout_print_rounding_0_001_ppm": delta <= Decimal("0.001")})
    assert len(nuclei) == 3 and {item["element"] for item in nuclei} == {"H", "C", "N"}
    assert [item["index_zero_based"] for item in nuclei] == [0, 1, 2]
    return nuclei


def coordinate_frame(xyz, stdout, nuclei):
    lines = xyz.splitlines()
    assert int(lines[0]) == 3
    coordinates = [line.split() for line in lines[2:] if line.strip()]
    output = stdout.splitlines()
    start = next(i for i, line in enumerate(output) if line == "CARTESIAN COORDINATES (ANGSTROEM)")
    printed = [output[start + 2 + i].split() for i in range(3)]
    assert len(coordinates) == 3
    order = [item["element"] for item in nuclei]
    assert [row[0] for row in coordinates] == [row[0] for row in printed] == order
    differences = [max(abs(Decimal(a) - Decimal(b)) for a, b in zip(row[1:], other[1:], strict=True))
                   for row, other in zip(coordinates, printed, strict=True)]
    return {"atom_order": order, "xyz": coordinates, "stdout_coordinates": printed,
            "max_printed_difference_angstrom": str(max(differences)),
            "same_frame_within_print_rounding": max(differences) <= Decimal("0.000001"),
            "note": "Full total Cartesian tensor in the exact input frame; no diagonalization or symmetrization."}


def originals_unchanged(originals):
    assert all(sha(path) == value for path, value in originals.items())


# Delivery adaptation is intentionally made only after reading the current
# candidate's exact CSV/JSON/script schema. No a011 Result ID, value, table field,
# selected method, atom order or old admission outcome is reused here.
