"""Read-only independent B-01 geometry and finite-sampling oracle.

Acceptance support, never imported by the product. No OPI, product parser,
scientific process, model call or production goal judgment is involved.
"""

from __future__ import annotations

import hashlib
import math
from pathlib import Path


def file_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _finite(value, label, *, positive=False, nonnegative=False):
    if (isinstance(value, bool) or not isinstance(value, (int, float))
            or not math.isfinite(value) or (positive and value <= 0)
            or (nonnegative and value < 0)):
        raise ValueError(f"invalid finite {label}")
    return value


def coordinates(path: Path):
    lines = path.read_text(encoding="utf-8").splitlines()
    rows = [line.split() for line in lines[2:] if line.strip()]
    if int(lines[0]) != 3 or [row[0] for row in rows] != ["O", "H", "H"]:
        raise ValueError("expected three atoms in fixed O,H,H order")
    points = [[float(value) for value in row[1:]] for row in rows]
    if any(len(p) != 3 or not all(math.isfinite(v) for v in p) for p in points):
        raise ValueError("invalid finite Cartesian coordinates")
    return points


def geometry_facts(path: Path):
    points = coordinates(path)
    o, a, b = points
    r1, r2 = math.dist(o, a), math.dist(o, b)
    if min(r1, r2) <= 0:
        raise ValueError("degenerate geometry")
    cosine = sum((a[i] - o[i]) * (b[i] - o[i]) for i in range(3)) / (r1 * r2)
    angle = math.degrees(math.acos(max(-1.0, min(1.0, cosine))))
    return {"r01_angstrom": r1, "r02_angstrom": r2, "angle_degrees": angle}


def verify_candidates(project: Path, manifest: dict):
    if manifest["units"] != "angstrom" or manifest["atom_mapping"] != {
        "index_base": 0, "elements": ["O", "H", "H"],
        "center_atom": 0, "scanned_atom": 1, "fixed_atom": 2,
    }:
        raise ValueError("units or atom mapping mismatch")
    source = manifest["source"]
    source_path = project / source["fixture"]
    if file_hash(source_path) != source["sha256"]:
        raise ValueError("source hash mismatch")
    reference_points = coordinates(source_path)
    reference = geometry_facts(source_path)
    tolerance = manifest["geometry_tolerances"]
    for name in ("distance_angstrom", "angle_degrees", "unchanged_coordinate_angstrom"):
        _finite(tolerance[name], name, positive=True)
    found = {}
    for window in manifest["windows"]:
        for name in ("center_angstrom", "h_angstrom", "target_width_angstrom"):
            _finite(window[name], name, positive=True)
        candidates = window["candidates"]
        if len(candidates) != 5:
            raise ValueError("exactly five registered candidates required")
        initial, optional = window["initial_candidate_ids"], window["optional_candidate_ids"]
        ids = [item["id"] for item in candidates]
        if (len(set(ids)) != 5 or len(initial) != 3 or len(optional) != 2
                or set(initial) & set(optional) or set(initial + optional) != set(ids)):
            raise ValueError("invalid required/optional candidate membership")
        for item in candidates:
            if item["id"] in found:
                raise ValueError("duplicate candidate identity")
            _finite(item["declared_r_angstrom"], "declared distance", positive=True)
            path = project / item["path"]
            if file_hash(path) != item["sha256"]:
                raise ValueError("candidate hash mismatch")
            actual = geometry_facts(path)
            points = coordinates(path)
            if abs(actual["r01_angstrom"] - item["declared_r_angstrom"]) > tolerance["distance_angstrom"]:
                raise ValueError("declared scan distance differs from XYZ")
            if abs(actual["r02_angstrom"] - reference["r02_angstrom"]) > tolerance["distance_angstrom"]:
                raise ValueError("fixed bond changed")
            if abs(actual["angle_degrees"] - reference["angle_degrees"]) > tolerance["angle_degrees"]:
                raise ValueError("fixed angle changed")
            if any(abs(points[a][i] - reference_points[a][i]) > tolerance["unchanged_coordinate_angstrom"]
                   for a in (0, 2) for i in range(3)):
                raise ValueError("fixed atom coordinates changed")
            if item["required_initial"] != (item["id"] in initial):
                raise ValueError("required member flag conflict")
            found[item["id"]] = actual
        ordered = sorted(candidates, key=lambda item: found[item["id"]]["r01_angstrom"])
        for item, factor in zip(ordered, [-1, -.5, 0, .5, 1], strict=True):
            expected = window["center_angstrom"] + factor * window["h_angstrom"]
            if abs(found[item["id"]]["r01_angstrom"] - expected) > tolerance["distance_angstrom"]:
                raise ValueError("window distances violate c + {-h,-h/2,0,h/2,h}")
        if set(initial) != {ordered[i]["id"] for i in (0, 2, 4)}:
            raise ValueError("initial members must be the three coarse points")
    return found


def sampling_judgment(ids, facts, energies, width, threshold, distance_tolerance):
    """Independent discrete goal judgment; boundary/tie is never a minimum claim."""
    _finite(width, "target width", positive=True)
    _finite(threshold, "energy threshold", positive=True)
    _finite(distance_tolerance, "distance tolerance", nonnegative=True)
    if len(set(ids)) != len(ids):
        raise ValueError("duplicate sampled candidate identity")
    if len(ids) < 3 or any(key not in energies for key in ids):
        return {"satisfied": False, "reason": "missing_required_energy"}
    if any(key not in facts for key in ids):
        return {"satisfied": False, "reason": "missing_required_geometry"}
    for key in ids:
        _finite(energies[key], "sampled energy")
        _finite(facts[key]["r01_angstrom"], "sampled distance", positive=True)
    ordered = sorted(ids, key=lambda key: facts[key]["r01_angstrom"])
    if any(facts[right]["r01_angstrom"] - facts[left]["r01_angstrom"] <= distance_tolerance
           for left, right in zip(ordered, ordered[1:])):
        raise ValueError("sampled distances are not distinct")
    minimum = min(ordered, key=energies.__getitem__)
    pos = ordered.index(minimum)
    if pos in (0, len(ordered) - 1):
        return {"satisfied": False, "reason": "boundary_minimum"}
    if any(energies[key] - energies[minimum] <= threshold for key in ordered if key != minimum):
        return {"satisfied": False, "reason": "not_numerically_distinct"}
    left, right = ordered[pos - 1], ordered[pos + 1]
    span = facts[right]["r01_angstrom"] - facts[left]["r01_angstrom"]
    return {
        "satisfied": span <= width + distance_tolerance,
        "reason": "sufficient_discrete_evidence" if span <= width + distance_tolerance else "span_too_wide",
        "minimum_candidate_id": minimum, "left_neighbor_id": left, "right_neighbor_id": right,
        "neighbor_span_angstrom": span,
        "minimum_neighbor_gap_eh": min(energies[left], energies[right]) - energies[minimum],
    }


def review_sampling(manifest, facts, energies, rounding_bounds):
    # All 15 raw values are printed to the same precision by the frozen engine.
    # 100 times the independent single-value rounding bound gives 50x headroom
    # over two-value subtraction rounding, not a chemical accuracy estimate.
    candidates = [item["id"] for window in manifest["windows"] for item in window["candidates"]]
    if len(candidates) != 15 or len(set(candidates)) != 15:
        raise ValueError("all 15 distinct reference candidates are required")
    for label, values in (("geometry", facts), ("energy", energies),
                          ("rounding bound", rounding_bounds)):
        if set(values) != set(candidates):
            raise ValueError(f"incomplete or unrelated reference {label}")
    for key in candidates:
        _finite(energies[key], "reference energy")
        _finite(facts[key]["r01_angstrom"], "reference distance", positive=True)
        _finite(rounding_bounds[key], "reference rounding bound", positive=True)
    threshold = 100 * max(rounding_bounds.values())
    if threshold <= 0 or not math.isfinite(threshold):
        raise ValueError("invalid output precision")
    tol = manifest["geometry_tolerances"]["distance_angstrom"]
    reviews = []
    for window in manifest["windows"]:
        initial = window["initial_candidate_ids"]
        width = window["target_width_angstrom"]
        h = window["h_angstrom"]
        initial_result = sampling_judgment(initial, facts, energies, width, threshold, tol)
        if "minimum_candidate_id" not in initial_result:
            raise ValueError("coarse center is not a distinct internal minimum")
        if initial_result["satisfied"]:
            action, chosen, final = "stop", None, initial_result
            if 2 * h > width + tol:
                raise ValueError("stop window is not sufficiently narrow")
        else:
            if not (2 * h > width and 1.5 * h <= width + tol):
                raise ValueError("refinement width conditions fail")
            ordered = sorted(initial, key=lambda key: facts[key]["r01_angstrom"])
            left, center, right = ordered
            if abs(energies[left] - energies[right]) <= threshold:
                raise ValueError("coarse neighbors do not distinguish a direction")
            action = "left" if energies[left] < energies[right] else "right"
            choices = sorted(window["optional_candidate_ids"], key=lambda key: facts[key]["r01_angstrom"])
            chosen, alternative = choices if action == "left" else choices[::-1]
            if energies[alternative] - energies[chosen] <= threshold:
                raise ValueError("independent midpoints do not confirm coarse direction")
            if energies[center] - energies[chosen] <= threshold:
                raise ValueError("chosen midpoint does not yield a distinct improvement")
            final = sampling_judgment(initial + [chosen], facts, energies, width, threshold, tol)
            if not final["satisfied"]:
                raise ValueError("one permitted refinement cannot satisfy frozen goal")
        reviews.append({"window_id": window["id"], "expected_action_from_independent_evidence": action,
                        "selected_candidate_id": chosen, "initial": initial_result, "after_action": final})
    if [item["expected_action_from_independent_evidence"] for item in reviews] != ["left", "right", "stop"]:
        raise ValueError("references do not establish the planned paired cases")
    return {"threshold_eh": threshold,
            "threshold_basis": "100 * largest(raw output quantum/2 + 8 * ulp(energy)) across 15 independent references; numerical distinction only, not model error",
            "windows": reviews}
