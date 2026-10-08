"""New checking intent, existing scientific predicate; no live execution."""

import copy

import pytest

from orca_agent.goals import validate_goal_evidence
from orca_agent.model_usage import current_basis
from orca_agent.models import OutputBinding, Plan, Request, Step
from orca_agent.report import build_report
from orca_agent.semantic import commit_candidate
from orca_agent.tools.analysis import SAMPLING_CHECK_VERSION, finite_sampling, sampling_check
from orca_agent.tools.dispatch import execute_call
from tests.unit.test_analysis import sampling_case
from tests.unit.test_delivery_snapshot import sampling_run
from tests.unit.test_semantic_control import candidate
from tests.unit.test_semantic_energy_coverage import synthetic_intake


@pytest.mark.parametrize("window_id", ["left", "right", "stop"])
def test_check_uses_the_same_predicate_and_never_publishes_sampling(window_id):
    arguments = sampling_case(window_id)
    scientific = finite_sampling(*arguments)
    assessed = sampling_check(*arguments)
    assert assessed["reason"] == scientific["reason"]
    assert assessed["goal_satisfied"] == scientific["goal_satisfied"]
    assert assessed["neighbor_span_angstrom"] == scientific["neighbor_span_angstrom"]
    assert assessed["predicate_checks"] == scientific["checks"]
    assert assessed["predicate_rule_version"] == "finite-sampling-1"
    assert assessed["assessment_status"] == "determinate"
    assert assessed["predicate_satisfied"] is (window_id == "stop")
    assert not assessed["qualified_outputs"]
    assert set(assessed["checked_artifact_outputs"]) == {"sampling_check"}


@pytest.mark.parametrize("reason", ["missing", "conditions", "geometry", "boundary", "indistinct"])
def test_negative_predicate_is_distinct_from_unreliable_inputs(reason):
    candidates, members, geometry, parameters = sampling_case("left")
    sampled = [member for member in members if member.evidence]
    if reason == "missing":
        sampled[0].evidence = None
    elif reason == "conditions":
        sampled[0].evidence.conditions.charge = 2
    elif reason == "geometry":
        geometry[candidates[0].artifact_id] = b"corrupt"
    elif reason == "boundary":
        sampled[0].evidence.energy_eh = -100
    else:
        sampled[2].evidence.energy_eh = sampled[1].evidence.energy_eh + parameters.energy_threshold_eh / 2
    result = sampling_check(candidates, members, geometry, parameters)
    assert result["operation_status"] == "completed"
    assert result["goal_satisfied"] is False
    assert bool(result["checked_artifact_outputs"]) is (reason in {"boundary", "indistinct"})
    assert result["predicate_satisfied"] is (False if reason in {"boundary", "indistinct"} else None)
    if reason in {"boundary", "indistinct"}:
        assert result["reason"] == {"boundary": "boundary_minimum", "indistinct": "not_numerically_distinct"}[reason]


@pytest.mark.parametrize("window", ["left", "stop"])
def test_production_artifact_check_completes_but_cannot_bind_acquisition(tmp_path, window):
    store, old_run, old_request, old_plan, _ = sampling_run(tmp_path, window)
    old_bytes = store.load_request(old_run).model_dump_json()
    goal = old_request.goals[0].model_copy(deep=True, update={"id": "check", "port": "sampling_check",
        "minimum_check_version": SAMPLING_CHECK_VERSION, "original_text": "Check whether sampling meets the criterion."})
    request = Request(original_text=goal.original_text, goals=[goal])
    step = Step.model_validate({**old_plan.steps[0].model_dump(mode="json"),
                                "tool": "analysis.sampling_check", "parameters": {"goal_id": goal.id}})
    plan = Plan(request_id=request.id, steps=[step], goal_map={goal.id: OutputBinding(step_id=step.id, port=goal.port)})
    permission = old_run.permission.model_copy(update={"allowed_tools": ["analysis.sampling_check"]})
    run = store.create_run(request, plan, permission, old_run.budget)
    result = execute_call(store, run, step.tool, {"goal_id": goal.id}, step=step)
    assert result.operation_status == "completed", result.diagnostics
    assert set(result.qualified_outputs) == {"sampling_check"}
    output = result.qualified_outputs["sampling_check"]
    assert output.value is None and output.unit is None and output.artifact_id in result.artifact_ids
    assert output.source["predicate_satisfied"] is (window == "stop")
    assert validate_goal_evidence(store, run, request, goal, result)
    acquisition = goal.model_copy(update={"port": "sampling", "minimum_check_version": "finite-sampling-1"})
    assert not validate_goal_evidence(store, run, request, acquisition, result)
    run.goal_status[goal.id] = "satisfied"
    store.save_run(run)
    report = build_report(store, run)
    assert report["user_goal_complete"]
    criterion = next(row["value"] for row in report["delivery"]["facts"] if row["kind"] == "criterion")
    assert criterion["assessment_status"] == "determinate"
    assert criterion["predicate_satisfied"] is (window == "stop")
    assert criterion["goal_satisfied"] is (window == "stop")
    assert criterion["predicate_rule_version"] == "finite-sampling-1"
    assert criterion["predicate_checks"][0]["status"] == ("passed" if window == "stop" else "failed")
    assert store.load_request(old_run).model_dump_json() == old_bytes
    assert not run.attempts and run.usage.orca_starts_actual == 0
    store.artifact_path(output.artifact_id).write_bytes(b"changed")
    assert not validate_goal_evidence(store, run, request, goal, result)
    assert not build_report(store, run)["user_goal_complete"]


@pytest.mark.parametrize("text,port", [
    ("检查已有有限采样是否达标。", "sampling_check"),
    ("取得达标的有限采样证据。", "sampling"),
    ("Check finite sampling without new calculations.", "sampling_check"),
])
def test_new_normalized_intent_preserves_missing_specification(tmp_path, text, port):
    store, run = synthetic_intake(tmp_path, text)
    parameters = candidate(store, run, kind="normalize", goals=[{"key": "sampling", "port": port, "text_basis": text}],
        questions=["Which registered sampling specification should be used?"],
        question_gaps={"Which registered sampling specification should be used?": ["missing:sampling_specification"]})
    changed = commit_candidate(store, run, parameters, decision_id="normalize_check", basis=current_basis(store, run))
    goal = store.load_request(changed).goals[0]
    assert goal.port == port and "missing:sampling_specification" in goal.unresolved
    assert not changed.attempts


@pytest.mark.parametrize("text,wrong", [
    ("取得达标的有限采样证据。", "sampling_check"),
    ("检查已有有限采样是否达标。", "sampling"),
    ("不要检查有限采样。", "sampling_check"),
    ("是否检查有限采样？", "sampling_check"),
    ("Should we check finite sampling?", "sampling_check"),
    ("Maybe check finite sampling.", "sampling_check"),
    ("也许检查有限采样。", "sampling_check"),
])
def test_mismatched_negative_or_question_cannot_activate_check_intent(tmp_path, text, wrong):
    store, run = synthetic_intake(tmp_path, text)
    before = store.load_request(run).model_dump_json()
    values = candidate(store, run, kind="normalize", goals=[{"key": "check", "port": wrong, "text_basis": text}],
                       notices=["Sampling specification is not registered."])
    with pytest.raises(ValueError, match="affirmative matching"):
        commit_candidate(store, run, values, decision_id="bad_intent", basis=current_basis(store, run))
    assert store.load_request(run).model_dump_json() == before


def test_two_explicit_intents_cannot_be_collapsed_to_checking_only(tmp_path):
    text = "检查有限采样是否达标。同时取得达标的有限采样证据。"
    store, run = synthetic_intake(tmp_path, text)
    values = candidate(store, run, kind="normalize", goals=[{"key": "check", "port": "sampling_check",
        "text_basis": "检查有限采样是否达标"}], notices=["Sampling specification is not registered."])
    with pytest.raises(ValueError, match="separate"):
        commit_candidate(store, run, values, decision_id="incomplete_goals", basis=current_basis(store, run))
    assert len(store.load_request(run).goals) == 1


def test_new_check_inherits_only_registered_sampling_specification(tmp_path):
    store, run, request, _, _ = sampling_run(tmp_path)
    text = "将目标改为检查有限采样是否达标。"
    message_id = store.enqueue_message(run.id, text)
    values = candidate(store, run, kind="replace_goals", replaces=[request.goals[0].id], goals=[{
        "key": "check", "port": "sampling_check", "text_basis": text, "message_id": message_id,
        "analysis_goal_ref": request.goals[0].id}])
    original = copy.deepcopy(request.goals[0].conditions)
    changed = commit_candidate(store, run, values, decision_id="user_check", basis=current_basis(store, run))
    new = store.load_request(changed).goals[0]
    assert new.port == "sampling_check" and new.conditions == original
    assert new.text_evidence["sampling_specification"]["source"] == "inherited"
    assert store.load_request_revision(changed, 1).goals[0].port == "sampling"


def test_multiple_registered_checks_cannot_omit_or_borrow_a_specification(tmp_path):
    store, old_run, old_request, _, _ = sampling_run(tmp_path)
    originals = [old_request.goals[0].model_copy(deep=True, update={"id": name}) for name in ("first", "second")]
    text = "将目标改为检查 first 有限采样是否达标。检查 second 有限采样是否达标。"
    request = Request(original_text="Obtain finite sampling evidence.", goals=originals)
    run = store.create_run(request, None, old_run.permission, old_run.budget)
    message_id = store.enqueue_message(run.id, text)
    first = {"key": "first_check", "port": "sampling_check", "text_basis": "检查 first 有限采样是否达标",
             "message_id": message_id, "analysis_goal_ref": "first"}
    parameters = candidate(store, run, kind="replace_goals", replaces=["first", "second"], goals=[first])
    with pytest.raises(ValueError, match="separate"):
        commit_candidate(store, run, parameters, decision_id="omitted", basis=current_basis(store, run))
    borrowed = {**first, "key": "second_check", "text_basis": "检查 second 有限采样是否达标"}
    parameters["goals"] = [first, borrowed]
    with pytest.raises(ValueError, match="unambiguous"):
        commit_candidate(store, run, parameters, decision_id="borrowed", basis=current_basis(store, run))
    parameters["goals"][1]["analysis_goal_ref"] = "second"
    changed = commit_candidate(store, run, parameters, decision_id="two_checks", basis=current_basis(store, run))
    assert [g.text_evidence["sampling_specification"]["goal_id"] for g in store.load_request(changed).goals] == ["first", "second"]


def test_unknown_sampling_specification_is_not_filled_from_defaults(tmp_path):
    text = "检查有限采样是否达标。阈值和候选未知，只登记此请求。"
    store, run = synthetic_intake(tmp_path, text)
    parameters = candidate(store, run, kind="normalize", goals=[{"key": "check", "port": "sampling_check",
        "text_basis": "检查有限采样是否达标"}],
        notices=["Checking intent registered; threshold and candidates remain unknown."])
    changed = commit_candidate(store, run, parameters, decision_id="unknown_check", basis=current_basis(store, run))
    goal = store.load_request(changed).goals[0]
    assert "missing:sampling_specification" in goal.unresolved
    assert not goal.conditions and not changed.calls and not changed.attempts
    assert changed.decisions[-1]["semantics"]["awaiting_reply"] is False


@pytest.mark.parametrize("amendment,retained", [
    ("阈值现在未知。", False), ("The threshold is now unknown.", False),
    ("将宽度改为 0.08。", False), ("Change the width to 0.08.", False),
    ("阈值不是未知。", True), ("The threshold is not unknown.", True),
    ("不要改变阈值。", True), ("Do not change the threshold.", True),
])
def test_later_specification_unknown_or_change_cannot_hide_behind_clipped_reference(tmp_path, amendment, retained):
    store, run, original, _, _ = sampling_run(tmp_path)
    clause = "将目标改为检查有限采样是否达标"
    message = store.enqueue_message(run.id, clause + "。" + amendment + "只登记此请求。")
    values = candidate(store, run, kind="replace_goals", replaces=[original.goals[0].id], goals=[{
        "key": "check", "port": "sampling_check", "text_basis": clause, "message_id": message,
        "analysis_goal_ref": original.goals[0].id}], notices=["Registered intent; unconfirmed specification stays unresolved."])
    changed = commit_candidate(store, run, values, decision_id="specification_update", basis=current_basis(store, run))
    goal = store.load_request(changed).goals[0]
    assert bool(goal.conditions) is retained
    assert ("missing:sampling_specification" in goal.unresolved) is (not retained)
    if retained:
        assert goal.conditions == original.goals[0].conditions


def test_separate_later_message_cannot_restore_an_explicitly_unknown_sampling_threshold(tmp_path):
    store, run, original, _, _ = sampling_run(tmp_path)
    clause = "将目标改为检查有限采样是否达标。"
    first = store.enqueue_message(run.id, clause)
    store.enqueue_message(run.id, "The threshold is now unknown. Registration only.")
    values = candidate(store, run, kind="replace_goals", replaces=[original.goals[0].id], goals=[{
        "key": "check", "port": "sampling_check", "text_basis": clause, "message_id": first,
        "analysis_goal_ref": original.goals[0].id}], notices=["Threshold remains unknown."])
    changed = commit_candidate(store, run, values, decision_id="later_unknown", basis=current_basis(store, run))
    goal = store.load_request(changed).goals[0]
    assert not goal.conditions and "missing:sampling_specification" in goal.unresolved
