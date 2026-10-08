"""Pure validation of proposed Request/Plan revisions before Store activation.

This module grants no permissions, writes no state and never infers scientific
success. The caller authenticates user message IDs against the control record.
"""

from __future__ import annotations

from orca_agent.models import EvidenceRef, Plan, Request, Run, Step
from orca_agent.tools.registry import get_tool


class PlanningError(ValueError):
    """The proposal cannot be activated under the current immutable contracts."""


def _require(condition, message):
    if not condition:
        raise PlanningError(message)


def _request_revision(prior: Request, proposed: Request, user_update: bool,
                      authenticated_user_revision: bool = False) -> bool:
    _require(proposed.id == prior.id, "Request identity cannot change")
    if proposed == prior:
        return False
    _require(proposed.version == prior.version + 1, "changed Request requires version +1")
    _require(user_update, "model cannot replace Request goals or physical conditions")
    _require(proposed.original_text == prior.original_text, "initial user text must be preserved")
    _require(proposed.messages[:len(prior.messages)] == prior.messages,
             "user message history is append-only")
    added = proposed.messages[len(prior.messages):]
    _require(bool(added), "Request changes require a new trusted user message")
    existing_ids = {message.get("id") for message in prior.messages}
    for message in added:
        _require(isinstance(message.get("id"), str) and bool(message["id"])
                 and message["id"] not in existing_ids and message.get("source") == "user"
                 and isinstance(message.get("text"), str) and bool(message["text"].strip())
                 and isinstance(message.get("created_at"), str) and bool(message["created_at"]),
                 "Request changes require the trusted control user-message format")
        existing_ids.add(message["id"])
    if authenticated_user_revision:
        return True
    old_goals = {goal.id: goal for goal in prior.goals if goal.required}
    new_goals = {goal.id: goal for goal in proposed.goals}
    for identifier, old in old_goals.items():
        _require(identifier in new_goals, "required goals cannot be removed by a revision flag")
        new = new_goals[identifier]
        clarified = (old.port == "unresolved" and old.minimum_check_version == "unresolved-1"
                     and "missing:goal_definition" in old.unresolved)
        _require(new.required and (new.port == old.port or clarified),
                 "required physical quantity cannot be removed or replaced")
        _require(clarified or _compatible_rule(old.minimum_check_version, new.minimum_check_version),
                 "required scientific checks cannot be lowered or substituted")
        _require(set(old.minimum_evidence).issubset(new.minimum_evidence),
                 "minimum required evidence cannot be removed")
        _require(new.original_text == old.original_text, "required goal's user-text basis is immutable")
    for name in ("charge", "multiplicity", "method", "basis", "geometry_artifact_id"):
        if getattr(prior, name) != getattr(proposed, name):
            source_name = "geometry" if name == "geometry_artifact_id" else name
            _require(proposed.conditions_source.get(source_name) == "explicit",
                     "user-changed physical conditions require explicit provenance")
    return True


def _compatible_rule(required: str, offered: str) -> bool:
    return required == offered or (required == "orca-hf-1" and offered == "orca-hf-2")


def _science(step: Step) -> bool:
    return "execute_orca" in get_tool(step.tool).effects


def _concrete_reference(reference: EvidenceRef, run: Run) -> None:
    if reference.producer_step_id:
        return
    own = reference.run_id == run.id and reference.result_id in run.result_ids
    _require(own or reference.result_id in run.permission.result_ids,
             "concrete Result is outside the permission snapshot")
    if reference.artifact_id:
        _require(own or reference.artifact_id in run.permission.artifact_ids,
                 "concrete Artifact is outside the permission snapshot")


def _step_permissions(step: Step, run: Run) -> None:
    tool = get_tool(step.tool)
    permission = run.permission
    _require(step.tool in permission.allowed_tools, "tool is outside the permission snapshot")
    if "execute_orca" in tool.effects:
        _require(permission.scientific_execution, "scientific execution is not authorized")
        _require(step.parameters.cores <= permission.max_cores
                 and step.parameters.memory_mb <= permission.max_memory_mb,
                 "scientific resources exceed the permission snapshot")
    if "query_external_identity" in tool.effects:
        _require(permission.external_identity_queries, "external identity queries are not authorized")
    if "prepare_geometry" in tool.effects:
        _require(permission.geometry_preparation, "geometry preparation is not authorized")
        _require(tool.max_cores <= permission.max_cores and tool.max_memory_mb <= permission.max_memory_mb,
                 "geometry preparation resources exceed the permission snapshot")
    if set(tool.effects) & {"create_artifact", "write_artifact", "copy_external_artifact",
                            "write_analysis", "import_artifact", "write_input_artifact"}:
        _require(permission.artifact_writes, "artifact creation is not authorized")
    if step.geometry and step.geometry.artifact_id:
        prepared = {binding.get("prepared_geometry", {}).get("artifact_id")
                    for binding in run.input_bindings.values()}
        _require(step.geometry.artifact_id in set(permission.artifact_ids) | prepared,
                 "geometry is outside the permission snapshot")
    parameters = step.parameters.model_dump(mode="json")
    if parameters.get("artifact_id") is not None:
        _require(parameters["artifact_id"] in permission.artifact_ids,
                 f"{step.tool}.parameters.artifact_id must be an authorized registered Artifact ID. "
                 "Step keys are not Artifact IDs; after importing, use the returned ID. "
                 "Keep a query whose Artifact is not yet available as a goal gap.")
    if parameters.get("source_id") is not None:
        _require(parameters["source_id"] in permission.source_ids,
                 "source ID is outside the permission snapshot")
    for reference in step.inputs.values():
        _concrete_reference(reference, run)


def _shape(step: Step, logical_by_id: dict[str, str], *, omit_scf=False):
    """Compare intent without allowing a renamed producer to appear as a new input."""
    data = step.model_dump(mode="json", exclude={"id", "logical_id"})
    geometry = data.get("geometry")
    if geometry and geometry.get("producer_step_id"):
        producer = geometry["producer_step_id"]
        geometry["producer_step_id"] = logical_by_id.get(producer, producer)
    data["depends_on"] = sorted(logical_by_id.get(item, item) for item in data["depends_on"])
    for reference in data["inputs"].values():
        if reference.get("producer_step_id"):
            producer = reference["producer_step_id"]
            reference["producer_step_id"] = logical_by_id.get(producer, producer)
    if omit_scf:
        data["parameters"].pop("scf_maxiter", None)
        # Graph/label changes do not make identical physical input a new intent.
        data.pop("system_id", None)
        data.pop("depends_on", None)
        if "geometry" in get_tool(step.tool).input_roles:
            data.pop("inputs", None)
    return data


def _repair(prior: Step, proposed: Step, run: Run, logical_by_id, user_change):
    _require(prior.logical_id == proposed.logical_id, "repair must retain its logical Step identity")
    _require(prior.tool == proposed.tool, "repair cannot replace the scientific tool")
    old, new = _shape(prior, logical_by_id), _shape(proposed, logical_by_id)
    old_parameters, new_parameters = old.pop("parameters"), new.pop("parameters")
    if not user_change:
        _require(old == new, "model repair cannot change physical inputs or dependencies")
    changed = {name for name in old_parameters | new_parameters
               if old_parameters.get(name) != new_parameters.get(name)}
    allowed = {"scf_maxiter"}
    if user_change:
        allowed |= {"charge", "multiplicity", "method", "basis"}
    _require(changed.issubset(allowed), "repair can only change explicitly allowed SCF settings")
    if "scf_maxiter" in changed:
        candidate = new_parameters["scf_maxiter"]
        _require(candidate in run.permission.allowed_repairs.get("scf_maxiter", [])
                 and candidate > old_parameters["scf_maxiter"],
                 "SCF repair candidate is not authorized")
    if proposed.id != prior.id:
        _require(old != new or bool(changed), "renaming unchanged scientific input is not a repair")


def _goal_bindings(request: Request, plan: Plan, run: Run):
    steps = {step.id: step for step in plan.steps}
    for goal in request.goals:
        binding = plan.goal_map.get(goal.id)
        if binding is None or binding.gap is not None:
            continue
        if binding.evidence:
            _concrete_reference(binding.evidence, run)
            _require(binding.evidence.port == goal.port,
                     "concrete goal evidence changes the requested physical quantity")
            _require(binding.evidence.rule_version is not None and _compatible_rule(
                goal.minimum_check_version, binding.evidence.rule_version),
                "concrete goal evidence lacks the minimum scientific rule")
        else:
            tool = get_tool(steps[binding.step_id].tool)
            _require(_compatible_rule(goal.minimum_check_version, tool.check_version),
                     "planned output cannot meet the required check version")
            if binding.port in tool.output_ports:
                _require(not goal.unresolved, "unresolved scientific goal cannot bind a qualified output")
    for step in plan.steps:
        tool = get_tool(step.tool)
        for role, reference in step.inputs.items():
            if not reference.producer_step_id:
                continue
            producer = get_tool(steps[reference.producer_step_id].tool)
            required = tool.required_input_checks.get(role, tool.required_input_checks.get(reference.port))
            if required:
                _require(_compatible_rule(required, producer.check_version),
                         "planned producer does not meet consumer scientific checks")


def validate_revision(prior_request: Request, prior_plan: Plan | None,
                      next_request: Request, next_plan: Plan | None, run: Run,
                      *, user_update: bool = False,
                      authenticated_user_revision: bool = False) -> None:
    """Validate a candidate against immutable purpose, launch history and permission.

    user_update is a caller-authenticated fact, not a model parameter. Even that
    flag cannot erase required goals or relax minimum evidence. Input/result hash
    checks and physical execution remain the Store/Tool consumption boundary.
    """
    # Revalidate copied/mutated Pydantic objects rather than trusting model_copy.
    prior_request = Request.model_validate(prior_request.model_dump())
    next_request = Request.model_validate(next_request.model_dump())
    prior_plan = Plan.model_validate(prior_plan.model_dump()) if prior_plan else None
    next_plan = Plan.model_validate(next_plan.model_dump()) if next_plan else None
    _require((prior_request.id, prior_request.version) == (run.request_id, run.request_version),
             "revision is based on a stale Request")
    prior_plan_identity = (prior_plan.id, prior_plan.version) if prior_plan else (None, None)
    _require(prior_plan_identity == (run.plan_id, run.plan_version),
             "revision is based on a stale Plan")
    if prior_plan and not authenticated_user_revision:
        prior_plan.validate_request(prior_request)
    user_change = _request_revision(prior_request, next_request, user_update,
                                    authenticated_user_revision)
    if next_plan is None:
        _require(prior_plan is None or user_change,
                 "active Plan may only be suspended by a trusted clarification")
        return
    next_plan.validate_request(next_request)
    if run.initial_science_steps is None:
        initial_systems = next_request.conditions.get("initial_system_ids")
        if initial_systems is not None:
            offered = [s.system_id for s in next_plan.steps if _science(s)]
            _require(len(offered) == len(initial_systems) and set(offered) == set(initial_systems),
                     "initial Plan must contain exactly the required initial scientific members")
        for step in next_plan.steps:
            if _science(step):
                for name, value in next_request.conditions.get("initial_parameters", {}).items():
                    _require(getattr(step.parameters, name, None) == value,
                             "initial scientific parameters differ from the user constraint")
    if prior_plan:
        _require(next_plan.id == prior_plan.id and next_plan.version == prior_plan.version + 1,
                 "Plan revision must preserve identity and increment version by one")
        _require(user_change or next_plan.model_dump(exclude={"version", "request_version"})
                 != prior_plan.model_dump(exclude={"version", "request_version"}),
                 "no-op Plan revision cannot consume a new version")
    else:
        history = [item for item in run.decisions if item.get("plan_version") is not None]
        history.extend({"plan_id": item["prior_plan_id"], "plan_version": item["prior_plan_version"]}
                       for item in run.decisions if item.get("prior_plan_version") is not None)
        if history:
            last = max(history, key=lambda item: item["plan_version"])
            _require(last.get("plan_id") == next_plan.id
                     and next_plan.version == last["plan_version"] + 1,
                     "resuming a suspended Plan must preserve historical identity and version")
        else:
            _require(next_plan.version == 1, "initial Plan requires version 1")
    historical = [attempt.frozen_step for attempt in run.attempts if attempt.frozen_step]
    historical.extend(call.frozen_step for call in run.calls if call.frozen_step)
    previous_steps = historical + (prior_plan.steps if prior_plan else [])
    previous_by_id = {step.id: step for step in previous_steps}
    frozen_by_id = {step.id: step for step in historical}
    previous_by_logical = {step.logical_id: step for step in previous_steps}
    logical_by_id = {step.id: step.logical_id for step in [*previous_steps, *next_plan.steps]}
    reserved_ids = {attempt.step_id for attempt in run.attempts} | {
        call.step_id for call in run.calls if call.step_id}
    pending_science, extra_science = 0, 0
    for step in next_plan.steps:
        _step_permissions(step, run)
        if step.id in reserved_ids:
            old = frozen_by_id.get(step.id, previous_by_id.get(step.id))
            _require(old is not None and old == step,
                     "reserved Step inputs and parameters are immutable; use a new repair Step")
        if not _science(step):
            continue
        old = previous_by_logical.get(step.logical_id)
        _require(old is not None or not any(attempt.logical_id == step.logical_id
                                           for attempt in run.attempts),
                 "historical Step snapshot is required to validate a repair")
        if old:
            prior_versions = [a.request_version for a in run.attempts
                              if a.logical_id == step.logical_id and a.request_version is not None]
            user_retargeted = bool(prior_versions and max(prior_versions) < next_request.version
                                   and next_request.messages
                                   and all(m.get("source") == "user" for m in next_request.messages))
            _repair(old, step, run, logical_by_id, user_change or user_retargeted)
        else:
            for previous in previous_steps:
                if _science(previous):
                    _require(_shape(previous, logical_by_id, omit_scf=True)
                             != _shape(step, logical_by_id, omit_scf=True),
                             "new logical ID cannot reset an existing scientific intent")
            deferred_initial = (run.science_baseline_policy == "first_science_plan"
                                and run.initial_science_steps is None)
            if (prior_plan is not None or run.initial_science_steps is not None) and not deferred_initial:
                _require(run.permission.allow_additional_science,
                         "additional scientific Steps are not authorized")
        if step.id not in reserved_ids:
            pending_science += 1
            logical_count = run.usage.logical_attempts.get(step.logical_id, 0)
            _require(logical_count < run.budget.attempts_per_step,
                     "logical Step attempt budget exhausted")
            if logical_count or (run.initial_science_steps is not None
                                 and step.logical_id not in run.initial_science_steps):
                extra_science += 1
    _require(pending_science <= run.budget.orca_starts - run.usage.orca_starts_reserved,
             "proposed Plan exceeds the remaining scientific startup budget")
    _require(extra_science <= run.budget.extra_orca_starts - run.usage.extra_orca_starts_reserved,
             "proposed Plan exceeds the remaining extra scientific startup budget")
    _goal_bindings(next_request, next_plan, run)
    if (run.science_baseline_policy == "first_science_plan" and run.initial_science_steps is None
            and any(_science(step) for step in next_plan.steps)):
        for goal in next_request.goals:
            if goal.required and goal.port in {"energy", "optimized_geometry", "dipole_moment"}:
                binding = next_plan.goal_map.get(goal.id)
                _require(binding is not None and binding.step_id is not None
                         and _science(next(s for s in next_plan.steps if s.id == binding.step_id)),
                         "first scientific Plan must cover the original required scientific goals")
                step = next(s for s in next_plan.steps if s.id == binding.step_id)
                if goal.port == "energy":
                    if "optimized_geometry" in get_tool(step.tool).output_ports:
                        relation = "optimized"
                    elif step.geometry.producer_step_id:
                        relation = ("optimized" if step.geometry.port == "optimized_geometry" else "fixed_initial")
                    else:
                        initial = {next_request.geometry_artifact_id,
                                   *(s.geometry_artifact_id for s in next_request.systems),
                                   *(b.get("prepared_geometry", {}).get("artifact_id")
                                     for b in run.input_bindings.values())} - {None}
                        relation = "fixed_initial" if step.geometry.artifact_id in initial else None
                    _require(relation is None or relation == goal.conditions.get("geometry_relation"),
                             "first scientific Plan cannot replace the requested geometry relation")
