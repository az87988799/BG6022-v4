"""Prepared read-only H2 audit primitives, never a property solver.

Importing this module reads no Run and runs no arithmetic. Use only after an
explicit settled notice. Actual ORCA response and delivered table adapters must
be confirmed against that candidate's raw format before final audit assembly.
No model, backend, subprocess, network, task-code execution, or source mutation.
"""

import hashlib
import json
import math
import re
from collections import Counter
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
FREEZE = ROOT / "docs/acceptance/code-as-tools-step7-refreeze-4/freeze"
NUMBER = re.compile(r"[-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[EeDd][-+]?\d+)?")
UNSETTLED = {"intent", "reserved", "running", "unknown"}


def read(path):
    return json.loads(path.read_bytes())


def sha(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def reference(path):
    return {"path": str(path.relative_to(ROOT)), "sha256": sha(path), "bytes": path.stat().st_size}


def decimal(text):
    assert isinstance(text, str) and NUMBER.fullmatch(text), "Preserve original finite numeric spelling"
    value = Decimal(text.replace("D", "E").replace("d", "e"))
    assert value.is_finite()
    return value


def authority():
    path = FREEZE / "holdout-selection.json"
    task = next(item for item in read(path)["tasks"] if item["id"] == "H2")
    assert task["fixed_requirements"]["rotation"]["axis_direction"] == [1, 2, 3]
    assert task["fixed_requirements"]["rotation"]["angle_degrees"] == 37
    return task, reference(path)


def settled_candidate(candidate):
    assert re.fullmatch(r"a[0-9]{3}", candidate)
    base = ROOT / "data/live-repair/attempts" / candidate
    start, capture, outcome = (read(base / name) for name in ("start.json", "attempt.json", "outcome.json"))
    assert start["candidate"] == capture["candidate"] == outcome["candidate"] == candidate
    assert start["task"] == capture["task"] == outcome["task"] == "H2"
    directory = base / "data/runs" / start["run_id"]
    run = read(directory / "run.json")
    assert run["id"] == start["run_id"] == outcome["run_id"]
    assert run["state"] == outcome["state"] and run["usage"] == outcome["usage"]
    assert not outcome["unsettled_execution"] and not outcome["unknown_fields"]
    assert not any(row["state"] in UNSETTLED for kind in ("attempts", "calls", "internal_calls") for row in run[kind])
    task, task_ref = authority()
    seed = read(FREEZE / "public-tasks.json")["seed_policy"]
    expected = task["original_text"] + seed["separator"] + seed["seed_suffix_template"].format(seed=1701)
    assert (base / "original.txt").read_text(encoding="utf-8") == expected
    assert sha(base / "original.txt") == capture["original_sha256"]
    before = {path: sha(path) for path in (base / "data").rglob("*") if path.is_file() and path.suffix != ".lock"}
    return base, directory, run, outcome, task, task_ref, before


def bound_source(base, run, result, file_key):
    """Only an exact archived Result key; never basename/latest-file discovery."""
    assert result["run_id"] == run["id"] and result["id"] in run["result_ids"]
    binding = result["source"]["files"][file_key]
    assert binding["artifact_id"] in result["artifact_ids"]
    data = base / "data"
    metadata_path = data / "artifacts" / binding["artifact_id"] / "artifact.json"
    metadata = read(metadata_path)
    assert metadata["id"] == binding["artifact_id"] and metadata["run_id"] == run["id"]
    assert metadata["attempt_id"] == result["attempt_id"]
    path = (data / metadata["path"]).resolve()
    assert path.is_relative_to(data.resolve()) and path.is_file()
    assert sha(path) == metadata["sha256"] == binding["sha256"]
    assert path.stat().st_size == metadata["size"]
    return path, {**reference(path), "artifact_id": metadata["id"], "result_id": result["id"],
        "attempt_id": result["attempt_id"], "source_file_key": file_key, "metadata": reference(metadata_path)}


def native_sources(base, directory, run):
    """Use one strict direct/SDK discovery chain for all independent audits.

    Rows retain separate completed/actual_started_confirmed facts and owned
    files as canonical-key -> (original-reference, bytes). Original keys remain
    in each reference. Static response/convergence still require raw review.
    """
    from native_raw_sources import discover_native_sources

    assert directory.resolve() == (base / "data/runs" / run["id"]).resolve()
    return discover_native_sources(base / "data", run, reference)


def xyz(raw):
    lines = raw.decode("utf-8").splitlines()
    count = int(lines[0])
    rows = [line.split() for line in lines[2:] if line.strip()]
    assert count == len(rows) == 4 and Counter(row[0] for row in rows) == {"C": 1, "H": 2, "O": 1}
    assert all(len(row) == 4 for row in rows)
    for row in rows:
        for value in row[1:]:
            decimal(value)
    return rows


def required_rotation(task):
    """Independent arithmetic for checking coordinates, not creating task inputs."""
    rotation = task["fixed_requirements"]["rotation"]
    assert rotation["sense"] == "right_hand_rule" and rotation["center"] == "original Cartesian coordinate origin"
    axis = rotation["axis_direction"]
    norm = math.sqrt(sum(float(x) ** 2 for x in axis))
    x, y, z = (float(value) / norm for value in axis)
    angle = math.radians(rotation["angle_degrees"])
    c, s, d = math.cos(angle), math.sin(angle), 1 - math.cos(angle)
    return [[c + x*x*d, x*y*d - z*s, x*z*d + y*s],
            [y*x*d + z*s, c + y*y*d, y*z*d - x*s],
            [z*x*d - y*s, z*y*d + x*s, c + z*z*d]]


def geometry_checks(original, rotated, task):
    assert [row[0] for row in original] == [row[0] for row in rotated], "Ordered atom mismatch"
    assert len(original) == len(rotated) == 4
    matrix = required_rotation(task)
    a = [[float(decimal(value)) for value in row[1:]] for row in original]
    b = [[float(decimal(value)) for value in row[1:]] for row in rotated]
    expected = [[sum(matrix[i][j] * row[j] for j in range(3)) for i in range(3)] for row in a]
    residual = max(abs(b[n][i] - expected[n][i]) for n in range(4) for i in range(3))
    distance_residual = max(abs(math.dist(a[i], a[j]) - math.dist(b[i], b[j]))
                            for i in range(4) for j in range(i))
    limits = task["tolerances"]["coordinates"]
    return {"convention": "column vectors; active r_rotated = R r_original; original origin; no fitted alignment",
        "required_rotation_matrix": matrix, "ordered_coordinate_max_abs_A": residual,
        "distance_max_abs_A": distance_residual,
        "specified_rotation_pass": residual <= limits["specified_rotation_abs_per_coordinate"],
        "internal_geometry_pass": distance_residual <= limits["pairwise_distance_preservation_abs"]}


def exact_numeric_cells(stdout, selectors):
    """Transcribe reviewer-selected raw cells; never infer an ORCA tensor layout.

    Each selector supplies {line, exact_line, numeric_index, text}; indexes count
    numeric tokens on that exact line. Review must independently establish the
    property heading, XYZ row/column order, static frequency, AU and CP-SCF
    convergence. This primitive proves literal transcription only.
    """
    lines = stdout.splitlines()
    values = []
    for cell in selectors:
        assert type(cell["line"]) is int and 1 <= cell["line"] <= len(lines)
        line = lines[cell["line"] - 1]
        assert line == cell["exact_line"]
        tokens = NUMBER.findall(line)
        assert type(cell["numeric_index"]) is int and 0 <= cell["numeric_index"] < len(tokens)
        assert tokens[cell["numeric_index"]] == cell["text"]
        decimal(cell["text"])
        values.append(cell["text"])
    assert len({(cell["line"], cell["numeric_index"]) for cell in selectors}) == len(selectors)
    return values


def tensor_checks(first_nine, first_printed_iso, second_nine, second_printed_iso, task):
    """Check all raw components, without symmetrizing, averaging or fitting R."""
    assert len(first_nine) == len(second_nine) == 9
    a = [[float(decimal(first_nine[3*i+j])) for j in range(3)] for i in range(3)]
    b = [[float(decimal(second_nine[3*i+j])) for j in range(3)] for i in range(3)]
    matrix = required_rotation(task)
    predicted = [[sum(matrix[i][k] * a[k][m] * matrix[j][m]
                      for k in range(3) for m in range(3)) for j in range(3)] for i in range(3)]
    residuals = [[b[i][j] - predicted[i][j] for j in range(3)] for i in range(3)]
    trace_errors = [abs(sum(decimal(values[4*i]) for i in range(3)) / 3 - decimal(iso))
                    for values, iso in ((first_nine, first_printed_iso), (second_nine, second_printed_iso))]
    iso_difference = abs(decimal(second_printed_iso) - decimal(first_printed_iso))
    t = task["tolerances"]
    trace_limit = Decimal(str(t["isotropic_polarizability"]["trace_over_three_consistency_abs"]))
    return {"raw_tensor_components": [first_nine, second_nine], "independently_printed_isotropic": [first_printed_iso, second_printed_iso],
        "predicted_rotated_tensor_for_comparison_only": predicted, "covariance_component_residuals": residuals,
        "trace_over_three_abs_errors": [str(x) for x in trace_errors], "isotropic_difference": str(iso_difference),
        "trace_consistency_pass": all(x <= trace_limit for x in trace_errors),
        "covariance_pass": max(abs(x) for row in residuals for x in row) <= t["cartesian_polarizability_tensor"]["rotation_covariance_max_abs_component"],
        "isotropic_invariance_pass": iso_difference <= Decimal(str(t["isotropic_polarizability"]["between_orientations_abs"])),
        "independent_reference": "not_run; unverified", "scientific_qualification_assigned": False}


def reported_value_checks(raw_values, reported_values, *, minimum_places, tolerance):
    assert len(raw_values) == len(reported_values)
    errors = [abs(decimal(raw) - decimal(reported)) for raw, reported in zip(raw_values, reported_values, strict=True)]
    places = [-decimal(value).as_tuple().exponent for value in reported_values]
    return {"component_abs_errors": [str(x) for x in errors], "effective_decimal_places": places,
        "transcription_pass": all(x <= Decimal(str(tolerance)) for x in errors),
        "machine_precision_pass": all(x >= minimum_places for x in places)}


def unchanged(original_hashes):
    return all(sha(path) == expected for path, expected in original_hashes.items())


# Intentionally no CLI or automatic passing result until a real H2 raw format
# and its delivery schema are inspected. See h2-raw-audit-readiness.json.
