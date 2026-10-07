"""Opt-in development probe using the same network/no-science pytest boundary."""

import os

import pytest

from tests.helpers.phase_b_model_evaluation import evaluate


@pytest.mark.model
def test_development_model_probe():
    label = os.environ.get("ORCA_AGENT_MODEL_PROBE")
    if not label:
        pytest.skip("no explicit development probe identity supplied")
    variant = os.environ.get("ORCA_AGENT_MODEL_PROBE_VARIANT", "V-07/discover-and-read")
    report = evaluate(variant, 1, allow_live=True, category="development", freeze_label=label,
                      model_profile=os.environ.get("ORCA_AGENT_MODEL_PROFILE", "disabled"))
    assert report["real_model_evidence_present"]
    assert report["safety_invariants_passed"]
    assert not report["fixture_gaps"]
    assert not [item for item in report["assertions"] if item["status"] == "failed"], report
