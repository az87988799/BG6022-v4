"""Program-owned purpose, permission and history gates for model proposals."""

import pytest

from orca_agent.models import (
    Attempt,
    BudgetLimits,
    Goal,
    InputRef,
    OutputBinding,
    PermissionSnapshot,
    Plan,
    Request,
    Run,
    Step,
    SystemInput,
)
from orca_agent.planning import PlanningError, validate_revision


def records(*, attempted=False):
    request = Request(id="request1", geometry_artifact_id="geometry1", goals=[
        Goal(id="energy", port="energy", minimum_check_version="orca-hf-2",
             minimum_evidence=["converged_scf"], original_text="energy at this geometry")])
    step = Step(id="sp1", logical_id="energy_sp", tool="orca.sp",
                geometry=InputRef(artifact_id="geometry1"), parameters={"scf_maxiter": 1})
    plan = Plan(id="plan1", request_id=request.id, steps=[step],
                goal_map={"energy": OutputBinding(step_id=step.id, port="energy")})
    run = Run(request_id=request.id, request_version=1, plan_id=plan.id, plan_version=1,
              permission=PermissionSnapshot(scientific_execution=True,
                  artifact_ids=["geometry1", "geometry2"], allowed_repairs={"scf_maxiter": [2, 100]}),
              budget=BudgetLimits(plan_revisions=2), initial_science_steps=["energy_sp"])
    if attempted:
        run.attempts = [Attempt(step_id=step.id, logical_id=step.logical_id, number=1,
                               tool=step.tool, geometry_artifact_id="geometry1",
                               input_fingerprint="input1", directory="attempts/one",
                               frozen_step=step.model_copy(deep=True), state="failed", started=True)]
        run.usage.orca_starts_reserved = 1
        run.usage.logical_attempts = {step.logical_id: 1}
    return request, plan, run


def revised(plan, *, scf=100, step_id="sp2", logical_id="energy_sp"):
    step = plan.steps[0].model_copy(deep=True, update={"id": step_id, "logical_id": logical_id})
    step.parameters.scf_maxiter = scf
    result = plan.model_copy(deep=True, update={"version": plan.version + 1, "steps": [step]})
    result.goal_map["energy"].step_id = step_id
    return result


def message(identifier="message1", text="Use the corrected geometry"):
    return {"id": identifier, "text": text, "source": "user", "created_at": "2026-10-06T10:00:00Z"}


def test_allowed_failure_repair_uses_new_step_and_retains_logical_budget_identity():
    request, plan, run = records(attempted=True)
    before = run.model_dump()
    validate_revision(request, plan, request, revised(plan), run)
    assert run.model_dump() == before
    assert plan.steps[0].parameters.scf_maxiter == 1
    assert run.attempts[0].frozen_step.parameters.scf_maxiter == 1


@pytest.mark.parametrize("update", [{"charge": 2}, {"method": "B3LYP"},
                                    {"geometry_artifact_id": "geometry2"}, {"original_text": "new goal"}])
def test_model_cannot_modify_request_purpose_or_physical_conditions(update):
    request, plan, run = records()
    candidate = request.model_copy(deep=True, update={"version": 2, **update})
    with pytest.raises(PlanningError, match="model cannot"):
        validate_revision(request, plan, candidate, None, run)


@pytest.mark.parametrize("change", ["remove", "quantity", "rule", "minimum", "optional"])
def test_user_update_flag_does_not_erase_required_goals(change):
    request, plan, run = records()
    request.goals.append(Goal(id="second", port="energy", minimum_check_version="orca-hf-2"))
    plan.goal_map["second"] = OutputBinding(step_id="sp1", port="energy")
    candidate = request.model_copy(deep=True, update={"version": 2, "messages": [message()]})
    if change == "remove":
        candidate.goals.pop(0)
    elif change == "quantity":
        candidate.goals[0].port = "free_energy"
    elif change == "rule":
        candidate.goals[0].minimum_check_version = "orca-hf-1"
    elif change == "minimum":
        candidate.goals[0].minimum_evidence = []
    else:
        candidate.goals[0].required = False
    with pytest.raises(PlanningError):
        validate_revision(request, plan, candidate, None, run, user_update=True)


def test_authentic_user_message_may_change_geometry_but_keeps_started_evidence_immutable():
    request, plan, run = records(attempted=True)
    candidate = request.model_copy(deep=True, update={"version": 2,
        "geometry_artifact_id": "geometry2", "messages": [message()]})
    new_plan = revised(plan, scf=1)
    new_plan.request_version = 2
    new_plan.steps[0].geometry.artifact_id = "geometry2"
    validate_revision(request, plan, candidate, new_plan, run, user_update=True)
    assert run.attempts[0].geometry_artifact_id == "geometry1"
    assert run.attempts[0].frozen_step.geometry.artifact_id == "geometry1"


@pytest.mark.parametrize("messages", [[], [{"id": "m1", "role": "user", "content": "change"}],
                                      [{**message(), "source": "artifact"}],
                                      [{**message(), "text": " "}]])
def test_user_revision_requires_control_message_provenance(messages):
    request, plan, run = records()
    candidate = request.model_copy(update={"version": 2, "messages": messages, "unresolved": ["state"]})
    with pytest.raises(PlanningError, match="trusted"):
        validate_revision(request, plan, candidate, None, run, user_update=True)


def test_message_history_and_initial_text_are_preserved():
    request, plan, run = records()
    request.messages = [message("original")]
    candidate = request.model_copy(update={"version": 2, "messages": [message("replacement")]})
    with pytest.raises(PlanningError, match="append-only"):
        validate_revision(request, plan, candidate, None, run, user_update=True)


@pytest.mark.parametrize("mutation", ["same_step", "logical", "candidate", "method", "rename"])
def test_repair_cannot_override_started_inputs_or_escape_frozen_choices(mutation):
    request, plan, run = records(attempted=True)
    new_plan = revised(plan)
    if mutation == "same_step":
        new_plan.steps[0].id = "sp1"
        new_plan.goal_map["energy"].step_id = "sp1"
    elif mutation == "logical":
        new_plan.steps[0].logical_id = "reset_budget"
        run.permission.allow_additional_science = True
    elif mutation == "candidate":
        new_plan.steps[0].parameters.scf_maxiter = 50
    elif mutation == "method":
        new_plan.steps[0].parameters.opt_maxiter = 20
    else:
        new_plan.steps[0].parameters.scf_maxiter = 1
    with pytest.raises(PlanningError):
        validate_revision(request, plan, request, new_plan, run)


@pytest.mark.parametrize("kind", ["request_id", "request_version", "plan_id", "plan_version", "basis"])
def test_revision_ids_and_versions_cannot_be_laundered(kind):
    request, plan, run = records()
    candidate = request.model_copy(deep=True)
    new_plan = revised(plan)
    if kind == "request_id":
        candidate.id = "another_request"
    elif kind == "request_version":
        candidate.version = 3
    elif kind == "plan_id":
        new_plan.id = "another_plan"
    elif kind == "plan_version":
        new_plan.version = 4
    else:
        run.plan_version = 2
    with pytest.raises(PlanningError):
        validate_revision(request, plan, candidate, new_plan, run)


def test_query_initial_plan_needs_no_scientific_authorization_or_budget():
    request = Request(id="query", goals=[Goal(id="read", port="text_window",
                                             minimum_check_version="evidence-read-1")])
    step = Step(id="read", logical_id="read", tool="evidence.text",
                parameters={"artifact_id": "artifact1"})
    plan = Plan(id="queryplan", request_id="query", steps=[step],
                goal_map={"read": OutputBinding(step_id="read", port="text_window")})
    run = Run(request_id=request.id, request_version=1,
              permission=PermissionSnapshot(allowed_tools=["evidence.text"], artifact_ids=["artifact1"]),
              budget=BudgetLimits(orca_starts=0, extra_orca_starts=0))
    validate_revision(request, None, request, plan, run)
    assert not run.attempts and run.usage.orca_starts_reserved == 0


def test_no_plan_clarification_and_explicit_user_update():
    request = Request(id="clarify", normalization_status="clarification", unresolved=["geometry"],
                      goals=[Goal(id="energy", port="energy", minimum_check_version="orca-hf-2")])
    run = Run(request_id=request.id, request_version=1)
    validate_revision(request, None, request, None, run)
    candidate = request.model_copy(update={"version": 2, "messages": [message()],
                                           "unresolved": ["electronic state"]})
    validate_revision(request, None, candidate, None, run, user_update=True)


def test_new_scientific_work_requires_additional_science_permission():
    request, plan, run = records()
    request.systems = [SystemInput(id="system1", geometry_artifact_id="geometry1"),
                       SystemInput(id="system2", geometry_artifact_id="geometry2")]
    request.goals[0].system_ids = ["system1"]
    plan.steps[0].system_id = "system1"
    additional = Step(id="new_sp", logical_id="new_geometry_energy", tool="orca.sp",
                      geometry=InputRef(artifact_id="geometry2"), system_id="system2")
    proposed = plan.model_copy(deep=True, update={"version": 2})
    proposed.steps.append(additional)
    with pytest.raises(PlanningError, match="additional scientific"):
        validate_revision(request, plan, request, proposed, run)
    run.permission.allow_additional_science = True
    validate_revision(request, plan, request, proposed, run)


@pytest.mark.parametrize("limit", ["attempts", "total", "extra"])
def test_repair_respects_cumulative_startup_budgets(limit):
    request, plan, run = records(attempted=True)
    if limit == "attempts":
        run.usage.logical_attempts["energy_sp"] = run.budget.attempts_per_step
    elif limit == "total":
        run.usage.orca_starts_reserved = run.budget.orca_starts
    else:
        run.usage.extra_orca_starts_reserved = run.budget.extra_orca_starts
    with pytest.raises(PlanningError, match="budget"):
        validate_revision(request, plan, request, revised(plan), run)


def test_all_tools_and_inputs_require_permission():
    request, plan, run = records()
    run.permission.allowed_tools = []
    with pytest.raises(PlanningError, match="tool is outside"):
        validate_revision(request, plan, request, revised(plan), run)
    run.permission.allowed_tools = ["orca.sp"]
    run.permission.artifact_ids = []
    with pytest.raises(PlanningError, match="geometry is outside"):
        validate_revision(request, plan, request, revised(plan), run)


def test_unknown_or_lower_check_cannot_satisfy_scientific_goal():
    request, plan, run = records()
    request.goals[0].minimum_check_version = "energy-compare-1"
    with pytest.raises(PlanningError, match="check version"):
        validate_revision(request, plan, request, revised(plan), run)


def test_model_can_retain_an_unavailable_goal_as_explicit_gap():
    request, plan, run = records()
    request.goals.append(Goal(id="free_energy", port="free_energy",
                              minimum_check_version="unresolved-1"))
    plan.goal_map["free_energy"] = OutputBinding(port="free_energy", gap="method unsupported")
    validate_revision(request, plan, request, revised(plan), run)


def test_removed_old_step_uses_frozen_history_for_repair_validation():
    request, old_plan, run = records(attempted=True)
    prior = old_plan.model_copy(deep=True, update={"version": 2})
    prior.steps = [Step(id="opt", logical_id="opt", tool="orca.opt",
                       geometry=InputRef(artifact_id="geometry1"))]
    prior.goal_map["energy"].step_id = "opt"
    run.plan_version = 2
    proposed = revised(old_plan)
    proposed.version = 3
    validate_revision(request, prior, request, proposed, run)


def test_noop_version_bump_is_rejected():
    request, plan, run = records()
    proposed = plan.model_copy(deep=True, update={"version": 2})
    with pytest.raises(PlanningError, match="no-op"):
        validate_revision(request, plan, request, proposed, run)


def test_new_system_alias_does_not_reset_physical_intent_budget():
    request, plan, run = records(attempted=True)
    request.systems = [SystemInput(id="alias", geometry_artifact_id="geometry1")]
    proposed = revised(plan, logical_id="new_budget_identity")
    proposed.steps[0].system_id = "alias"
    run.permission.allow_additional_science = True
    with pytest.raises(PlanningError, match="new logical ID"):
        validate_revision(request, plan, request, proposed, run)
