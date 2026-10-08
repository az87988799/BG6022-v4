"""Offline grounding of input acquisition versus the requested energy relation."""

import pytest
from test_repair_cycle_e2e_capacity import _fixed_text
from test_semantic_control import candidate
from test_text_entry import text_environment

from orca_agent.config import Config
from orca_agent.model_usage import current_basis
from orca_agent.natural import initialize_text
from orca_agent.semantic import _text_geometry_relation, commit_candidate
from orca_agent.store import Store
from tests.helpers import phase_b_repair_cycle_execution as execution


@pytest.mark.parametrize("system", ["water", "methane"])
def test_fixed_e2e_original_text_normalizes_without_guessing_geometry_relation(tmp_path, system):
    config = execution._e2e_profile(Config(), category="development")
    store = Store(tmp_path / "data", environment_root=tmp_path / "environment")
    text = _fixed_text(system)
    run = initialize_text(store, config, text)
    ports = ["optimized_geometry", "energy"] if system == "water" else ["energy"]
    relation = "optimized" if system == "water" else "fixed_initial"
    run = commit_candidate(store, run, candidate(store, run, kind="normalize",
        goals=[{"key": port, "port": port, "system_refs": [system], "text_basis": text,
                "geometry_relation": relation} for port in ports],
        conditions={key: {"value": value, "source": "explicit", "text_basis": text}
                    for key, value in config.text.defaults.items()}),
        decision_id="exact_e2e_text", basis=current_basis(store, run))
    request = store.load_request(run)
    assert request.original_text == text and request.normalization_status == "normalized"
    assert {goal.port for goal in request.goals} == set(ports)
    assert all(goal.conditions["geometry_relation"] == relation for goal in request.goals)
    assert not run.calls and not run.attempts and not run.model_records
    assert not run.result_ids and request.systems[0].geometry_artifact_id is None


@pytest.mark.parametrize(("text", "relation"), [
    ("取得水的初始几何，然后优化水并报告优化后的电子能。", "optimized"),
    ("Obtain the initial geometry of water, optimize it and report its electronic energy.", "optimized"),
    ("取得水的初始几何的电子能。", "fixed_initial"),
    ("Obtain the initial geometry electronic energy of water.", "fixed_initial"),
    ("取得水的初始几何。", None),
    ("Prepare the initial geometry of water.", None),
    ("水的固定几何做单点电子能，不得称为优化结构。", "fixed_initial"),
    ("Report water single-point electronic energy and do not call it optimized geometry.", "fixed_initial"),
    ("取得水的初始几何，做单点并优化后报告电子能。", None),
    ("Prepare the initial geometry of water, report single-point and optimized electronic energy.", None),
    ("水做优化并报告电子能，不得称为优化结构。", None),
    ("不得优化水，登记电子能。", None),
    ("水的几何关系未知，登记电子能。", None),
])
def test_acquisition_and_negative_labels_preserve_relation_boundaries(tmp_path, text, relation):
    store, config = text_environment(tmp_path)
    run = initialize_text(store, config, text)
    request = store.load_request(run)
    assert _text_geometry_relation(request, [{"text": text}], ["water"]) == relation


@pytest.mark.parametrize("update", [
    "水的几何关系现在未知。", "The geometry relation for water is now unknown.",
    "不得称为优化结构。", "Do not call it optimized geometry.",
])
def test_later_unknown_or_denied_optimized_relation_does_not_keep_old_choice(tmp_path, update):
    store, config = text_environment(tmp_path)
    text = "Optimize water and report its electronic energy."
    run = initialize_text(store, config, text)
    request = store.load_request(run)
    assert _text_geometry_relation(request, [{"text": text}, {"text": update}], ["water"]) is None
