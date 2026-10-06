"""User-authored structured input mapped into program-owned identities."""

from pathlib import Path

from pydantic import Field, model_validator

from orca_agent.models import (
    BudgetLimits,
    CalculationParameters,
    Goal,
    Identifier,
    InputRef,
    OutputBinding,
    Plan,
    Record,
    Request,
    Step,
    new_id,
)
from orca_agent.tools.registry import validate_geometry, validate_parameters
from orca_agent.versions import CURRENT_CHECK_VERSION


class StepInput(Record):
    name: Identifier
    tool: str
    parameters: CalculationParameters
    geometry_from: Identifier | None = None
    depends_on: list[Identifier] = Field(default_factory=list)


class GoalInput(Record):
    name: Identifier
    step: Identifier
    port: str
    required: bool = True


class TaskInput(Record):
    description: str
    geometry: str
    steps: list[StepInput] = Field(min_length=1, max_length=4)
    goals: list[GoalInput] = Field(min_length=1)
    budget: BudgetLimits = Field(default_factory=BudgetLimits)

    @model_validator(mode="after")
    def deterministic_permissions(self):
        if self.budget.model_calls or self.budget.plan_revisions:
            raise ValueError("fixed structured requests cannot grant model or revision activity")
        return self


def prepare_task(spec_path: Path, store):
    spec_path = spec_path.resolve(strict=True)
    if spec_path.stat().st_size > 65536:
        raise ValueError("structured request exceeds 64 KiB")
    task = TaskInput.model_validate_json(spec_path.read_text(encoding="utf-8"))
    source = (spec_path.parent / task.geometry).resolve(strict=True)
    if Path(task.geometry).is_absolute() or not source.is_relative_to(spec_path.parent):
        raise ValueError("geometry must be a file within the structured request directory")
    if source.stat().st_size > 65536:
        raise ValueError("geometry exceeds 64 KiB")
    xyz = source.read_text(encoding="utf-8")
    names = {item.name for item in task.steps}
    if len(names) != len(task.steps):
        raise ValueError("step aliases must be unique")
    for item in task.steps:
        validate_parameters(item.tool, item.parameters)
        validate_geometry(xyz, item.parameters)
        if (item.geometry_from and item.geometry_from not in names
                or any(dep not in names for dep in item.depends_on)):
            raise ValueError("unknown producer/dependency alias")
    if any(goal.step not in names for goal in task.goals):
        raise ValueError("goal refers to an unknown step alias")
    geometry = store.import_artifact(source, role="initial_geometry")
    settings = task.steps[0].parameters
    request = Request(
        original_text=task.description, geometry_artifact_id=geometry.id,
        charge=settings.charge, multiplicity=settings.multiplicity,
        method=settings.method, basis=settings.basis,
        conditions_source={name: ("explicit" if name in settings.model_fields_set else "default")
                           for name in ("charge", "multiplicity", "method", "basis")}
        | {"geometry": "explicit"},
        goals=[Goal(id=goal.name, port=goal.port, required=goal.required,
                    minimum_check_version=CURRENT_CHECK_VERSION) for goal in task.goals],
    )
    step_ids = {item.name: new_id("step") for item in task.steps}
    steps = []
    for item in task.steps:
        dependencies = list(dict.fromkeys(item.depends_on + (
            [item.geometry_from] if item.geometry_from else [])))
        reference = (InputRef(producer_step_id=step_ids[item.geometry_from],
                              port="optimized_geometry") if item.geometry_from
                     else InputRef(artifact_id=geometry.id))
        steps.append(Step(id=step_ids[item.name], logical_id=step_ids[item.name], tool=item.tool,
                          parameters=item.parameters, geometry=reference,
                          depends_on=[step_ids[name] for name in dependencies]))
    plan = Plan(request_id=request.id, request_version=request.version, steps=steps,
                goal_map={goal.name: OutputBinding(step_id=step_ids[goal.step], port=goal.port)
                          for goal in task.goals})
    plan.validate_request(request)
    return request, plan, task.budget
