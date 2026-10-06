"""75 frozen real-model evaluations; default offline CI skips every transmission.

Use --live-model explicitly. Re-running uses the same durable slot and never
recreates its HTTP budget. Explanation review is independent and required for
acceptance; a stored unreviewed trajectory is reported as unverified, not passed.
"""

import importlib.util
import os
from pathlib import Path

import pytest

PROJECT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("phase_b_model_evaluation", PROJECT / "tests/helpers/phase_b_model_evaluation.py")
EVALUATION = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(EVALUATION)


@pytest.mark.model
@pytest.mark.parametrize("variant", EVALUATION.cases.fixed_variant_ids())
@pytest.mark.parametrize("repetition", (1, 2, 3))
def test_fixed_evidence_model(request, variant, repetition):
    report = EVALUATION.evaluate(variant, repetition, allow_live=request.config.getoption("--live-model"),
                                freeze_label=os.environ.get("ORCA_AGENT_EVAL_FREEZE", "formal-v1"))
    assert report["safety_invariants_passed"], "purpose, permission, budget or original evidence changed"
    if report["fixture_gaps"]:
        pytest.skip("real-source fixture gap; no HTTP sent and evaluation remains unverified")
    failures = [item["metric"] for item in report["assertions"] if item["status"] == "failed"]
    assert not failures, f"frozen acceptance assertions failed: {failures}"
    if report["status"] != "passed":
        pytest.skip("trajectory archived; independent explanation/behavior review remains required")
    assert report["real_model_evidence_present"]
