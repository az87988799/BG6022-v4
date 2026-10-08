"""Translate symbolic model intentions into program-owned Plan identities."""

import re
from typing import Annotated, Any, Literal

from pydantic import Field, ValidationError

from orca_agent.models import EvidenceRef, InputRef, OutputBinding, Plan, Record, Step, new_id
from orca_agent.tools.registry import get_tool, validate_parameters

_CALL_TOOL_FORMS = ({"step_id": str}, {"tool": str, "parameters": dict})


ShortRef = Annotated[str, Field(strict=True, min_length=1, max_length=64)]


class GoalExplanation(Record):
    goal_ref: ShortRef
    fact_refs: list[ShortRef] = Field(min_length=1, max_length=64)
    explanation_ref: ShortRef
    blocker_refs: list[ShortRef] = Field(max_length=32)
    next_action_ref: ShortRef


class TerminalExplanation(Record):
    version: Literal["terminal-delivery-1"]
    snapshot_ref: Literal["current"]
    goal_explanations: list[GoalExplanation] = Field(min_length=1, max_length=16)


class StopParameters(Record):
    reason: str = Field(default="", strict=True, max_length=1000)
    delivery: TerminalExplanation | None = None


class ContractStopParameters(StopParameters):
    """The schema advertised by new requests; legacy parameters stay readable."""

    delivery: TerminalExplanation


class ClarifyParameters(Record):
    questions: list[Annotated[str, Field(strict=True, min_length=1, max_length=1000)]] = Field(min_length=1, max_length=5)
    unresolved: list[Annotated[str, Field(strict=True, min_length=1, max_length=1000)]] = Field(min_length=1, max_length=5)


def action_parameter_schema(action, *, immediate=True, terminal_required=False):
    if action == "stop":
        return (ContractStopParameters if terminal_required else StopParameters).model_json_schema()
    if action == "clarify":
        return ClarifyParameters.model_json_schema()
    if action == "call_tool":
        return call_tool_parameters_schema(immediate=immediate)
    if action in {"initial_plan", "revise_plan"}:
        return ProposedPlan.model_json_schema()
    raise ValueError("action parameters are owned by their specialized contract")


def validate_action_parameters(action, values, *, terminal_required=False):
    """Shared structural validation; applicability/permission remain separate."""
    if action == "call_tool":
        if not valid_call_tool_parameters(values):
            raise ProposalError("call_tool needs exactly one declared parameter shape.",
                                path=["parameters"], shapes=call_tool_parameter_shapes())
    elif action in {"stop", "clarify"}:
        model = ((ContractStopParameters if terminal_required else StopParameters)
                 if action == "stop" else ClarifyParameters)
        try:
            model.model_validate(values)
        except ValidationError as exc:
            error = _schema_error(exc, path=["parameters"])
            if action == "clarify":
                schema = action_parameter_schema(action)
                questions = schema["properties"]["questions"]
                error.detail.update(path=["parameters"], required_fields=schema["required"],
                    min_items=questions["minItems"], max_items=questions["maxItems"],
                    min_string_length=questions["items"]["minLength"],
                    max_string_length=questions["items"]["maxLength"])
            raise error from None


def call_tool_parameter_shapes(*, immediate=True):
    """Exact field sets shared by schema, prose and correction diagnostics."""
    return [list(form) for form in (_CALL_TOOL_FORMS if immediate else _CALL_TOOL_FORMS[:1])]


def call_tool_instruction(*, immediate=True):
    shapes = ["{" + ",".join(keys) + "}" for keys in call_tool_parameter_shapes(immediate=immediate)]
    text = f"call_tool Plan Step={shapes[0]} only; no inline params; match reason."
    if immediate:
        text += f" Or {shapes[1]} for an immediate reader; tool=catalog.name, never effects."
    return text


def call_tool_parameters_schema(*, immediate=True):
    """One structural contract; scientific parameters remain the Tool's job."""
    types = {str: "string", dict: "object"}
    forms = _CALL_TOOL_FORMS if immediate else _CALL_TOOL_FORMS[:1]
    alternatives = [{"required": list(form), "maxProperties": len(form)} for form in forms]
    return {"type": "object", "properties": {
        field: {"type": types[kind]} for form in forms for field, kind in form.items()},
        **(alternatives[0] if len(alternatives) == 1 else {"oneOf": alternatives})}


def valid_call_tool_parameters(values):
    """Exact disjoint forms forbid inline overrides of an existing Step."""
    return isinstance(values, dict) and any(
        set(values) == set(form) and all(isinstance(values[field], kind) for field, kind in form.items())
        for form in _CALL_TOOL_FORMS)


class ProposalError(ValueError):
    """Program-authored correction detail; never includes rejected input values."""

    def __init__(self, requirement, **details):
        super().__init__(requirement)
        self.detail = {"requirement": requirement, **details}


def _schema_error(exc, *, path, tool=None):
    def location(part):
        if isinstance(part, int):
            return part
        return part if re.fullmatch(r"[A-Za-z_][A-Za-z_0-9]{0,63}", str(part)) else "[field]"

    errors = [{"type": error["type"], "loc": [*path, *(location(part) for part in error["loc"])]}
              for error in exc.errors(include_input=False, include_context=False, include_url=False)[:8]]
    return ProposalError("Use the declared parameter schema at each listed path.",
                         **({"tool": tool} if tool else {}), errors=errors)


class ProposedStep(Record):
    key: str
    logical_key: str | None = None
    tool: str | None = None
    parameters: dict[str, Any] = Field(default_factory=dict)
    system_id: str | None = None
    geometry: dict[str, Any] | None = None
    depends_on: list[str] = Field(default_factory=list)
    inputs: dict[str, dict[str, Any]] = Field(default_factory=dict)


class ProposedPlan(Record):
    steps: list[ProposedStep] = Field(min_length=1, max_length=8)
    goal_map: dict[str, dict[str, Any]]


def materialize_plan(store, run, parameters):
    try:
        proposal = ProposedPlan.model_validate(parameters)
    except ValidationError as exc:
        raise _schema_error(exc, path=["parameters"]) from None
    prior = store.load_plan(run)
    request = store.load_request(run)
    if run.input_bindings:
        from orca_agent.input_bindings import resolved_request
        request = resolved_request(store, run, request)
    missing = [goal for goal in request.goals if goal.required and goal.id not in proposal.goal_map]
    if missing:
        raise ProposalError(
            "Map every required Request goal. When evidence or capability is unavailable, retain the goal "
            "with its exact port and an explicit gap; a gap records unmet evidence, not goal completion.",
            path=["parameters", "goal_map"],
            missing_goals=[{"goal_id": goal.id, "port": goal.port} for goal in missing],
            gap_bindings={goal.id: {"port": goal.port, "gap": "<describe missing evidence or capability>"}
                          for goal in missing})
    existing = {s.id: s for s in prior.steps} if prior else {}
    history = {a.frozen_step.id: a.frozen_step for a in run.attempts if a.frozen_step}
    history.update({c.frozen_step.id: c.frozen_step for c in run.calls if c.frozen_step})
    history.update(existing)
    keys = [s.key for s in proposal.steps]
    if len(set(keys)) != len(keys):
        raise ValueError("proposal Step keys must be unique")
    ids = {key: key if key in history else new_id("step") for key in keys}
    known_logical = {s.logical_id for s in history.values()}
    logical = {}
    for item in proposal.steps:
        label = item.logical_key or item.key
        logical[item.key] = (history[item.key].logical_id if item.key in history else
                             label if label in known_logical else new_id("logical"))
    steps = []
    for index, item in enumerate(proposal.steps):
        if item.tool is None:
            if item.key not in history or set(item.model_fields_set) != {"key"}:
                raise ValueError("retained Step must reference its existing key without changes")
            steps.append(history[item.key].model_copy(deep=True))
            continue
        try:
            definition = get_tool(item.tool)
        except ValueError:
            raise ProposalError("Choose a Tool from the current permitted catalog.",
                                path=["parameters", "steps", index, "tool"]) from None
        artifact = item.parameters.get("artifact_id")
        future_artifact = (isinstance(artifact, str) and artifact not in run.permission.artifact_ids
                           and (artifact in ids or artifact in ids.values()))
        if future_artifact or isinstance(artifact, dict) and "gap" in artifact:
            raise ProposalError(
                "artifact_id needs an existing registered ID, not a Step key or gap. "
                "Put {gap,port} only in parameters.goal_map[Goal.id]; omit Steps needing a future Artifact "
                "until the import Result returns its ID, then revise_plan.", tool=definition.name,
                path=["parameters", "steps", index, "parameters", "artifact_id"],
                goal_gap_shape={"parameters": {"goal_map": {
                    "<Goal.id>": {"port": "<unchanged Goal.port>", "gap": "<missing evidence>"}}}})
        try:
            validate_parameters(item.tool, item.parameters)
        except ValidationError as exc:
            raise _schema_error(exc, path=["parameters", "steps", index, "parameters"],
                                tool=definition.name) from None
        except ValueError as exc:
            raise ProposalError(str(exc), tool=definition.name,
                                path=["parameters", "steps", index, "parameters"]) from None
        geometry = item.geometry
        if geometry and "producer_key" in geometry:
            if set(geometry) != {"producer_key", "port"}:
                raise ValueError("future geometry has ambiguous or undeclared fields")
            geometry = {"producer_step_id": ids[geometry["producer_key"]],
                        "port": geometry.get("port")}
        if geometry is None:
            if "execute_orca" in get_tool(item.tool).effects:
                system = next((s for s in request.systems if s.id == item.system_id), None)
                artifact = system.geometry_artifact_id if system else request.geometry_artifact_id
                if artifact:
                    geometry = {"artifact_id": artifact}
        inputs = {}
        for name, value in item.inputs.items():
            if "producer_key" in value:
                value = {**value, "producer_step_id": ids[value["producer_key"]]}
                value.pop("producer_key")
            inputs[name] = EvidenceRef.model_validate(value)
        dependencies = [ids[key] for key in item.depends_on]
        # A reference itself explicitly declares the dependency relationship.
        dependencies += [r.producer_step_id for r in inputs.values() if r.producer_step_id]
        if geometry and geometry.get("producer_step_id"):
            dependencies.append(geometry["producer_step_id"])
        steps.append(Step(id=ids[item.key], logical_id=logical[item.key], tool=item.tool,
                          parameters=item.parameters, system_id=item.system_id,
                          geometry=InputRef.model_validate(geometry) if geometry else None,
                          depends_on=list(dict.fromkeys(dependencies)), inputs=inputs))
    goals = {}
    requested_goals = {goal.id: goal for goal in request.goals}
    for goal_id, value in proposal.goal_map.items():
        if goal_id in requested_goals and value.get("port") != requested_goals[goal_id].port:
            raise ProposalError("A goal binding must preserve its requested physical quantity or query port. "
                "If that output is not yet available, use {gap,port} with the same port.",
                path=["parameters", "goal_map", goal_id, "port"], expected=requested_goals[goal_id].port)
        if "step_key" in value:
            value = {**value, "step_id": ids[value["step_key"]]}
            value.pop("step_key")
        goals[goal_id] = OutputBinding.model_validate(value)
    previous = [d for d in run.decisions if d.get("plan_version") is not None]
    previous += [{"plan_id": d["prior_plan_id"], "plan_version": d["prior_plan_version"]}
                 for d in run.decisions if d.get("prior_plan_version") is not None]
    last = max(previous, key=lambda d: d["plan_version"]) if previous else {}
    return Plan(id=prior.id if prior else last.get("plan_id") or new_id("plan"),
                version=prior.version + 1 if prior else last.get("plan_version", 0) + 1,
                request_id=request.id, request_version=request.version, steps=steps, goal_map=goals)
