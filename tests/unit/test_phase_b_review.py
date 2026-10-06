"""Negative controls for the independent acceptance oracle, not product features."""

import copy
import importlib.util
import json
from pathlib import Path

import pytest

PROJECT = Path(__file__).resolve().parents[2]
MANIFEST = PROJECT / "tests/fixtures/phase_b/sampling-candidates.json"
SPEC = importlib.util.spec_from_file_location(
    "phase_b_review", PROJECT / "tests/helpers/phase_b_review.py")
REVIEW = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(REVIEW)
file_hash = REVIEW.file_hash
sampling_judgment = REVIEW.sampling_judgment
verify_candidates = REVIEW.verify_candidates


def test_frozen_candidate_bytes_and_actual_one_dimensional_coordinates():
    facts = verify_candidates(PROJECT, json.loads(MANIFEST.read_text(encoding="utf-8")))
    assert len(facts) == 15


@pytest.mark.parametrize("kind", ["hash", "label", "fixed_bond", "angle", "mapping", "members"])
def test_incorrect_candidate_rejected_even_when_hash_is_recomputed(tmp_path, kind):
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    manifest = copy.deepcopy(manifest)
    manifest["source"]["fixture"] = str(PROJECT / manifest["source"]["fixture"])
    for window in manifest["windows"]:
        for item in window["candidates"]:
            item["path"] = str(PROJECT / item["path"])
    item = manifest["windows"][0]["candidates"][0]
    if kind == "hash":
        item["sha256"] = "0" * 64
    elif kind == "label":
        item["declared_r_angstrom"] += .01
    elif kind == "mapping":
        manifest["atom_mapping"]["scanned_atom"] = 2
    elif kind == "members":
        manifest["windows"][0]["initial_candidate_ids"][0] = "nonexistent"
    else:
        lines = Path(item["path"]).read_text().splitlines()
        row = lines[4 if kind == "fixed_bond" else 3].split()
        row[2] = str(float(row[2]) + .03)
        lines[4 if kind == "fixed_bond" else 3] = " ".join(row)
        changed = tmp_path / "changed.xyz"
        changed.write_text("\n".join(lines) + "\n")
        item["path"], item["sha256"] = str(changed), file_hash(changed)
        if kind == "angle":
            item["declared_r_angstrom"] = REVIEW.geometry_facts(changed)["r01_angstrom"]
    with pytest.raises(ValueError):
        verify_candidates(PROJECT, manifest)


@pytest.mark.parametrize("values,reason", [
    ([0., 1., 2.], "boundary_minimum"),
    ([1e-13, 0., 1.], "not_numerically_distinct"),
    ([1., 0., 2.], "span_too_wide"),
])
def test_insufficient_sampling_never_becomes_satisfied(values, reason):
    ids = ["a", "b", "c"]
    facts = {key: {"r01_angstrom": value} for key, value in zip(ids, [1., 2., 3.])}
    result = sampling_judgment(ids, facts, dict(zip(ids, values)), .15, 1e-10, 1e-8)
    assert not result["satisfied"] and result["reason"] == reason


@pytest.mark.parametrize("value", [float("nan"), float("inf"), -float("inf"), True])
def test_nonfinite_or_boolean_energy_cannot_pass_sampling(value):
    ids = ["a", "b", "c"]
    facts = {key: {"r01_angstrom": distance} for key, distance in zip(ids, [1., 2., 3.])}
    with pytest.raises(ValueError, match="finite sampled energy"):
        sampling_judgment(ids, facts, {"a": 1., "b": 0., "c": value}, 3., 1e-10, 1e-8)


@pytest.mark.parametrize("field,value", [("width", float("nan")), ("threshold", -1.),
                                         ("tolerance", float("inf"))])
def test_invalid_sampling_thresholds_are_rejected(field, value):
    values = {"width": 3., "threshold": 1e-10, "tolerance": 1e-8, field: value}
    ids = ["a", "b", "c"]
    facts = {key: {"r01_angstrom": distance} for key, distance in zip(ids, [1., 2., 3.])}
    with pytest.raises(ValueError, match="finite"):
        sampling_judgment(ids, facts, {"a": 1., "b": 0., "c": 2.},
                          values["width"], values["threshold"], values["tolerance"])


def test_sampling_missing_or_duplicate_sources_cannot_pass():
    ids = ["a", "b", "c"]
    facts = {key: {"r01_angstrom": distance} for key, distance in zip(ids, [1., 2., 3.])}
    energies = {"a": 1., "b": 0., "c": 2.}
    assert sampling_judgment(ids, facts, {"a": 1., "b": 0.}, 3., 1e-10, 1e-8) == {
        "satisfied": False, "reason": "missing_required_energy"}
    assert sampling_judgment(ids, {"a": facts["a"]}, energies, 3., 1e-10, 1e-8) == {
        "satisfied": False, "reason": "missing_required_geometry"}
    with pytest.raises(ValueError, match="duplicate"):
        sampling_judgment(["a", "b", "b"], facts, energies, 3., 1e-10, 1e-8)
    facts["c"]["r01_angstrom"] = 2.
    with pytest.raises(ValueError, match="distances are not distinct"):
        sampling_judgment(ids, facts, energies, 3., 1e-10, 1e-8)


@pytest.mark.parametrize("kind", ["tolerance", "declared", "window"])
def test_nonfinite_manifest_values_cannot_disable_geometry_checks(kind):
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    if kind == "tolerance":
        manifest["geometry_tolerances"]["distance_angstrom"] = float("nan")
    elif kind == "declared":
        manifest["windows"][0]["candidates"][0]["declared_r_angstrom"] = float("nan")
    else:
        manifest["windows"][0]["h_angstrom"] = float("nan")
    with pytest.raises(ValueError, match="finite"):
        verify_candidates(PROJECT, manifest)


def synthetic_review_data():
    # Analytic test values exercise this helper only; never reference expected
    # energies for the real ORCA/Agent acceptance cases.
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    facts = verify_candidates(PROJECT, manifest)
    center = REVIEW.geometry_facts(PROJECT / manifest["source"]["fixture"])["r01_angstrom"]
    energies = {key: (value["r01_angstrom"] - center) ** 2 for key, value in facts.items()}
    return manifest, facts, energies, {key: 1e-12 for key in facts}


def test_independent_direction_comes_from_energy_and_stop_from_width():
    data = synthetic_review_data()
    review = REVIEW.review_sampling(*data)
    assert [item["expected_action_from_independent_evidence"] for item in review["windows"]] == [
        "left", "right", "stop"]
    assert all(item["after_action"]["satisfied"] for item in review["windows"])
    assert review["windows"][2]["selected_candidate_id"] is None
    manifest, facts, energies, rounding = data
    left, _, right = manifest["windows"][0]["initial_candidate_ids"]
    energies[left], energies[right] = energies[right], energies[left]
    with pytest.raises(ValueError, match="midpoints do not confirm coarse direction"):
        REVIEW.review_sampling(manifest, facts, energies, rounding)


@pytest.mark.parametrize("field", ["facts", "energies", "rounding"])
def test_reference_review_requires_all_fifteen_candidates(field):
    manifest, facts, energies, rounding = synthetic_review_data()
    key = manifest["windows"][2]["optional_candidate_ids"][0]
    {"facts": facts, "energies": energies, "rounding": rounding}[field].pop(key)
    with pytest.raises(ValueError, match="incomplete or unrelated"):
        REVIEW.review_sampling(manifest, facts, energies, rounding)


@pytest.mark.parametrize("field", ["facts", "energies", "rounding"])
def test_reference_review_rejects_nonfinite_optional_stop_evidence(field):
    manifest, facts, energies, rounding = synthetic_review_data()
    key = manifest["windows"][2]["optional_candidate_ids"][0]
    if field == "facts":
        facts[key]["r01_angstrom"] = float("nan")
    else:
        {"energies": energies, "rounding": rounding}[field][key] = float("nan")
    with pytest.raises(ValueError, match="finite reference"):
        REVIEW.review_sampling(manifest, facts, energies, rounding)
