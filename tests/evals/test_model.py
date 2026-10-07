"""75 frozen real-model evaluations; default offline CI skips every transmission.

Use --live-model explicitly. Re-running uses the same durable slot and never
recreates its HTTP budget. Explanation review is independent and required for
acceptance; a stored unreviewed trajectory is reported as unverified, not passed.
"""

import importlib.util
import os
from pathlib import Path

import pytest

from tests.helpers.phase_b_grading import classify_grade

PROJECT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("phase_b_model_evaluation", PROJECT / "tests/helpers/phase_b_model_evaluation.py")
EVALUATION = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(EVALUATION)


@pytest.mark.model
@pytest.mark.parametrize("variant", EVALUATION.cases.evaluation_variant_ids())
@pytest.mark.parametrize("repetition", (1, 2, 3))
def test_fixed_evidence_model(request, variant, repetition):
    report = EVALUATION.evaluate(variant, repetition, allow_live=request.config.getoption("--live-model"),
                                freeze_label=os.environ.get("ORCA_AGENT_EVAL_FREEZE", "formal-v1"))
    status = classify_grade(report)
    assert status != "failed", f"frozen acceptance checks failed: {report}"
    if status != "passed":
        pytest.skip(f"evaluation {status}: missing required response, fixture or independent review evidence")
