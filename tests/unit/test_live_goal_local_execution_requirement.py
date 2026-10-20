"""Real a006 Goal text replay against inert task archives; no calculation runs."""

import json
from pathlib import Path

import pytest

from orca_agent.models import Goal, SystemInput
from orca_agent.task_observations import _requires_native_execution, assess
from tests.unit.test_reported_observation_policy import enable
from tests.unit.test_task_observation_conditions import pure_file_case

FIXTURE = Path(__file__).parents[1] / "fixtures/code_contracts/live-t2-a006-local-obligations.json"


def retained(port):
    data = json.loads(FIXTURE.read_text(encoding="utf-8"))
    return Goal.model_validate(next(item for item in data["goals"] if item["port"] == port))


@pytest.mark.parametrize("port", ["candidate_structures", "selected_conformers", "search_report"])
def test_broad_authenticated_source_does_not_transfer_other_goals_execution_requirement(tmp_path, monkeypatch, port):
    store, run, request, archive_goal, result = pure_file_case(tmp_path, monkeypatch)
    goal = retained(port)
    original = goal.model_dump(mode="json")
    assert "ORCA" in goal.original_text
    assert not _requires_native_execution(goal)
    # Rebind only the inert archive's identifiers, keeping actual local text.
    goal = goal.model_copy(update={"id": archive_goal.id, "port": archive_goal.port})
    request.systems = [SystemInput(id=goal.system_ids[0])]
    request.goals = [goal]
    enable(run)
    answer = assess(store, run, request, goal, result)
    assert answer["status"] == "passed", answer
    assert answer["scientific_qualification"] is False
    assert all(item["status"] == "unverified" for item in answer["minimum_evidence"])
    assert retained(port).model_dump(mode="json") == original


def test_actual_native_goal_cannot_be_fulfilled_by_task_json(tmp_path, monkeypatch):
    store, run, request, archive_goal, result = pure_file_case(tmp_path, monkeypatch)
    goal = retained("orca_single_point_energy")
    assert _requires_native_execution(goal)
    goal = goal.model_copy(update={"id": archive_goal.id, "port": archive_goal.port})
    request.systems = [SystemInput(id=goal.system_ids[0])]
    request.goals = [goal]
    enable(run)
    answer = assess(store, run, request, goal, result)
    assert answer["status"] == "unresolved"
    assert "explicit_native_execution_requires_native_source" in answer["reasons"]


def test_no_local_quantity_or_requirement_retains_original_native_obligation():
    goal = retained("orca_single_point_energy")
    goal.quantity, goal.minimum_evidence = None, []
    assert _requires_native_execution(goal)


@pytest.mark.parametrize("text", [
    "不要运行ORCA优化，但必须完成真实ORCA单点能量计算。",
    "Do not run ORCA optimization, but perform a real ORCA single-point calculation.",
])
def test_local_negative_clause_does_not_waive_later_affirmative_execution(text):
    goal = retained("orca_single_point_energy")
    goal.quantity = text
    goal.minimum_evidence = []
    assert _requires_native_execution(goal)
