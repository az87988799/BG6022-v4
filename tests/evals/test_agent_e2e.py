"""Three formal repetitions of the six frozen real model + ORCA trajectories."""

import json
import os

import pytest

from orca_agent.store import Store, atomic_write
from tests.helpers.phase_b_freeze import validate_freeze
from tests.helpers.phase_b_grade_joint import grade_joint
from tests.helpers.phase_b_joint import ROOT, run_case

CASES = ("repair_success", "repair_exhaustion", "sampling_left", "sampling_right",
         "sampling_stop", "methane_opt_control")


@pytest.mark.e2e
@pytest.mark.parametrize("case", CASES)
@pytest.mark.parametrize("repetition", (1, 2, 3))
def test_joint_frozen(case, repetition):
    label = os.environ.get("ORCA_AGENT_EVAL_FREEZE", "formal-v1")
    freeze = validate_freeze(label)
    identity = f"{label}-{case}-{repetition}"
    run, metadata = run_case(case, "formal", identity, live_model=True, live_orca=True)
    report = grade_joint(Store(ROOT / "agent"), run, case, metadata=metadata)
    report["freeze"] = freeze
    destination = ROOT / "evaluations" / f"{identity}.grade.json"
    atomic_write(destination, (json.dumps(report, ensure_ascii=False, indent=2) + "\n").encode())
    assert report["passed"], json.dumps(report, ensure_ascii=False)
