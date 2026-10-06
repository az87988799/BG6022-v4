"""The seven persisted objects and their small, typed local records.

Scientific conditions are deliberately narrow. Extending this profile requires a
registry/adapter/check change and new evidence, not free-form ORCA input.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
from typing import Annotated, Any, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator

from orca_agent.versions import LEGACY_CHECK_VERSION

Identifier = Annotated[str, Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]{0,95}$")]
Port = Annotated[str, Field(pattern=r"^[A-Za-z][A-Za-z0-9_]{0,63}$")]
CheckStatus = Literal["passed", "failed", "unverified", "not_applicable"]


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid4().hex}"


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class Record(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class CalculationParameters(Record):
    method: Literal["HF"] = "HF"
    basis: Literal["STO-3G"] = "STO-3G"
    charge: Literal[0] = 0
    multiplicity: Literal[1] = 1
    cores: Annotated[int, Field(strict=True, ge=1, le=4)] = 4
    memory_mb: Annotated[int, Field(strict=True, ge=256, le=1024)] = 1024
    maxcore_mb: Annotated[int, Field(strict=True, ge=1, le=192)] = 192
    scf_maxiter: Annotated[int, Field(strict=True, ge=1, le=300)] = 100
    opt_maxiter: Annotated[int, Field(strict=True, ge=1, le=200)] = 100
    timeout_seconds: Annotated[float, Field(gt=0, le=900)] = 300

    @model_validator(mode="after")
    def within_memory(self) -> CalculationParameters:
        if self.cores * self.maxcore_mb > self.memory_mb:
            raise ValueError("aggregate MaxCore exceeds the requested total memory")
        return self


class InputRef(Record):
    artifact_id: Identifier | None = None
    producer_step_id: Identifier | None = None
    port: Literal["optimized_geometry"] | None = None

    @model_validator(mode="after")
    def unambiguous(self) -> InputRef:
        direct = self.artifact_id is not None
        future = self.producer_step_id is not None and self.port is not None
        if direct == future or (direct and (self.producer_step_id or self.port)):
            raise ValueError("geometry must bind an artifact OR a producer step and port")
        if not direct and not future:
            raise ValueError("geometry reference is incomplete")
        return self


class Goal(Record):
    id: Identifier
    port: Port
    required: bool = True
    # Missing fields in historical files mean the old rule, never the current rule.
    minimum_check_version: Literal[
        "orca-hf-1", "orca-hf-2", "evidence-read-1", "energy-compare-1",
        "finite-sampling-1", "unresolved-1",
    ] = LEGACY_CHECK_VERSION
    original_text: str = ""
    system_ids: list[Identifier] = Field(default_factory=list)
    conditions: dict[str, Any] = Field(default_factory=dict)
    minimum_evidence: list[str] = Field(default_factory=list)
    unresolved: list[str] = Field(default_factory=list)


class EvidenceRef(Record):
    """A local typed consumer binding; it owns no independent lifecycle."""

    producer_step_id: Identifier | None = None
    run_id: Identifier | None = None
    result_id: Identifier | None = None
    attempt_id: Identifier | None = None
    artifact_id: Identifier | None = None
    sha256: str | None = None
    port: Port = "energy"
    rule_version: str | None = None

    @model_validator(mode="after")
    def exact_or_future(self):
        if self.producer_step_id:
            if any((self.run_id, self.result_id, self.attempt_id, self.artifact_id, self.sha256)):
                raise ValueError("future reference cannot also select concrete evidence")
        elif not (self.run_id and self.result_id):
            raise ValueError("concrete evidence must identify its Run and Result")
        return self


class SystemInput(Record):
    id: Identifier
    geometry_artifact_id: Identifier | None = None
    conditions: dict[str, Any] = Field(default_factory=dict)
    conditions_source: dict[str, str] = Field(default_factory=dict)
    atom_mapping: list[int] = Field(default_factory=list)
    label: str = ""


class OutputBinding(Record):
    step_id: Identifier | None = None
    port: Port
    evidence: EvidenceRef | None = None
    gap: str | None = None

    @model_validator(mode="after")
    def one_source(self):
        if sum(value is not None for value in (self.step_id, self.evidence, self.gap)) != 1:
            raise ValueError("goal needs one future output, existing evidence, or explicit gap")
        return self


class Request(Record):
    id: Identifier = Field(default_factory=lambda: new_id("request"))
    version: Annotated[int, Field(ge=1)] = 1
    original_text: str = "Structured local calculation"
    geometry_artifact_id: Identifier | None = None
    charge: int | None = 0
    multiplicity: int | None = 1
    method: str | None = "HF"
    basis: str | None = "STO-3G"
    conditions_source: dict[str, Literal["explicit", "default", "inherited", "inferred"]] = Field(
        default_factory=lambda: {
            "charge": "explicit", "multiplicity": "explicit", "method": "explicit",
            "basis": "explicit", "geometry": "explicit",
        }
    )
    goals: Annotated[list[Goal], Field(min_length=1)]
    systems: Annotated[list[SystemInput], Field(max_length=5)] = Field(default_factory=list)
    messages: list[dict[str, Any]] = Field(default_factory=list)
    unresolved: list[str] = Field(default_factory=list)
    conditions: dict[str, Any] = Field(default_factory=dict)
    normalization_status: Literal["structured", "pending", "clarification", "normalized"] = "structured"

    @model_validator(mode="after")
    def unique_goals(self) -> Request:
        if len({goal.id for goal in self.goals}) != len(self.goals):
            raise ValueError("goal identities must be unique")
        if not any(goal.required for goal in self.goals):
            raise ValueError("a scientific request requires at least one mandatory goal")
        if len({item.id for item in self.systems}) != len(self.systems):
            raise ValueError("system identities must be unique")
        return self


class Step(Record):
    id: Identifier
    logical_id: Identifier
    tool: str
    parameters: Any = Field(default_factory=CalculationParameters)
    geometry: InputRef | None = None
    depends_on: list[Identifier] = Field(default_factory=list)
    inputs: dict[str, EvidenceRef] = Field(default_factory=dict)
    system_id: Identifier | None = None

    @model_validator(mode="after")
    def registered_parameters(self):
        from orca_agent.tools.registry import get_tool, validate_parameters
        self.parameters = validate_parameters(self.tool, self.parameters)
        if "execute_orca" in get_tool(self.tool).effects and self.geometry is None:
            raise ValueError("scientific tool requires geometry")
        if "execute_orca" not in get_tool(self.tool).effects and self.geometry is not None:
            raise ValueError("non-scientific tool cannot claim a scientific geometry binding")
        return self


class Plan(Record):
    id: Identifier = Field(default_factory=lambda: new_id("plan"))
    version: Annotated[int, Field(ge=1)] = 1
    request_id: Identifier
    request_version: Annotated[int, Field(ge=1)] = 1
    steps: Annotated[list[Step], Field(min_length=1, max_length=8)]
    goal_map: dict[str, OutputBinding]

    @model_validator(mode="after")
    def valid_graph(self) -> Plan:
        from orca_agent.tools.registry import get_tool
        steps = {step.id: step for step in self.steps}
        if len(steps) != len(self.steps):
            raise ValueError("step identities must be unique")
        if len({s.logical_id for s in self.steps}) != len(self.steps):
            raise ValueError("logical step identities must be unique in a fixed plan")
        for step in self.steps:
            if len(set(step.depends_on)) != len(step.depends_on):
                raise ValueError("duplicate dependencies")
            if any(dep not in steps for dep in step.depends_on):
                raise ValueError("dependency references an unknown step")
            producer = step.geometry.producer_step_id if step.geometry else None
            if producer:
                if producer not in step.depends_on:
                    raise ValueError("future geometry producer must be an explicit dependency")
                if step.geometry.port not in get_tool(steps[producer].tool).output_ports:
                    raise ValueError("producer does not declare optimized_geometry")
            for reference in step.inputs.values():
                if reference.producer_step_id:
                    if reference.producer_step_id not in step.depends_on:
                        raise ValueError("future evidence producer must be an explicit dependency")
                    if reference.port not in get_tool(steps[reference.producer_step_id].tool).output_ports:
                        raise ValueError("producer does not declare the required port")
        visited: set[str] = set()
        active: set[str] = set()

        def visit(step_id: str) -> None:
            if step_id in active:
                raise ValueError("cyclic plan")
            if step_id in visited:
                return
            active.add(step_id)
            for dependency in steps[step_id].depends_on:
                visit(dependency)
            active.remove(step_id)
            visited.add(step_id)

        for step_id in steps:
            visit(step_id)
        for binding in self.goal_map.values():
            if binding.step_id is not None and binding.step_id not in steps:
                raise ValueError("goal binding references an unknown step")
            if (binding.step_id and binding.port not in
                    (get_tool(steps[binding.step_id].tool).output_ports
                     + get_tool(steps[binding.step_id].tool).observation_outputs)):
                raise ValueError("step does not produce the goal's port")
        return self

    def validate_request(self, request: Request) -> None:
        from orca_agent.tools.registry import get_tool
        if (self.request_id, self.request_version) != (request.id, request.version):
            raise ValueError("plan is based on a different request revision")
        goals = {goal.id: goal for goal in request.goals}
        if set(self.goal_map) - set(goals):
            raise ValueError("plan introduces unknown goals")
        for goal in request.goals:
            binding = self.goal_map.get(goal.id)
            if goal.required and binding is None:
                raise ValueError("required goal is not covered")
            if binding and binding.port != goal.port:
                raise ValueError("plan changes the requested physical quantity")
        for step in self.steps:
            if "execute_orca" not in get_tool(step.tool).effects:
                continue
            if request.unresolved:
                raise ValueError("unresolved request conditions block scientific planning")
            system = next((s for s in request.systems if s.id == step.system_id), None)
            if step.system_id and system is None:
                raise ValueError("step references an unknown requested system")
            for name in ("charge", "multiplicity", "method", "basis"):
                required = (system.conditions.get(name, getattr(request, name))
                            if system else getattr(request, name))
                if getattr(step.parameters, name) != required:
                    raise ValueError(f"step changes request condition: {name}")
            allowed = ({system.geometry_artifact_id} if system else
                       {request.geometry_artifact_id})
            if step.geometry.artifact_id and step.geometry.artifact_id not in allowed:
                raise ValueError("direct geometry must bind the request's initial geometry")


class Tool(Record):
    name: str
    description: str
    parameter_schema: dict[str, Any]
    input_roles: list[str] = Field(default_factory=lambda: ["geometry"])
    output_ports: list[Port]
    observation_outputs: list[str] = Field(default_factory=list)
    effects: list[str] = Field(
        default_factory=lambda: ["read_geometry", "write_attempt", "execute_orca"]
    )
    max_cores: int = 4
    max_memory_mb: int = 1024
    check_version: str = LEGACY_CHECK_VERSION
    implementation: str
    required_input_checks: dict[str, str] = Field(default_factory=dict)
    check_contract: dict[str, Any] = Field(default_factory=dict)


class PermissionSnapshot(Record):
    version: Annotated[int, Field(ge=1)] = 1
    scientific_execution: bool = False
    allowed_tools: list[str] = Field(
        default_factory=lambda: ["orca.sp", "orca.opt"]
    )
    max_cores: Annotated[int, Field(ge=1, le=4)] = 4
    max_memory_mb: Annotated[int, Field(ge=256, le=1024)] = 1024
    artifact_ids: list[Identifier] = Field(default_factory=list)
    source_ids: list[Identifier] = Field(default_factory=list)
    result_ids: list[Identifier] = Field(default_factory=list)
    model_execution: bool = False
    artifact_writes: bool = False
    allowed_repairs: dict[str, list[int]] = Field(default_factory=dict)
    allow_additional_science: bool = False

    @model_validator(mode="after")
    def registered_tools(self):
        from orca_agent.tools.registry import get_tool
        for name in self.allowed_tools:
            get_tool(name)
        return self


class BudgetLimits(Record):
    attempts_per_step: Annotated[int, Field(ge=1, le=3)] = 3
    orca_starts: Annotated[int, Field(ge=0, le=4)] = 4
    extra_orca_starts: Annotated[int, Field(ge=0, le=3)] = 3
    postprocess_starts: Literal[0] = 0
    run_seconds: Annotated[float, Field(gt=0, le=1800)] = 1800
    model_calls: Annotated[int, Field(ge=0, le=8)] = 0
    plan_revisions: Annotated[int, Field(ge=0, le=2)] = 0
    model_tokens: Annotated[int, Field(ge=0, le=48000)] = 0
    input_tokens: Annotated[int, Field(ge=0, le=12000)] = 0
    output_tokens: Annotated[int, Field(ge=0, le=2000)] = 0
    decision_rounds: Annotated[int, Field(ge=0, le=12)] = 0
    evidence_reads: Annotated[int, Field(ge=0, le=24)] = 0
    analysis_executions: Annotated[int, Field(ge=0, le=8)] = 0
    corrections_per_proposal: Annotated[int, Field(ge=0, le=1)] = 0
    transport_retries: Annotated[int, Field(ge=0, le=1)] = 0


class BudgetUsage(Record):
    orca_starts_reserved: Annotated[int, Field(ge=0)] = 0
    orca_starts_actual: Annotated[int, Field(ge=0)] = 0
    # Actual over-budget execution must remain representable as a failure fact.
    postprocess_starts: Annotated[int, Field(strict=True, ge=0)] = 0
    elapsed_seconds: Annotated[float, Field(ge=0)] = 0
    cpu_seconds: Annotated[float, Field(ge=0)] = 0
    resource_usage_complete: bool = True
    logical_attempts: dict[str, int] = Field(default_factory=dict)
    fingerprint_attempts: dict[str, int] = Field(default_factory=dict)
    extra_orca_starts_reserved: Annotated[int, Field(ge=0)] = 0
    model_calls: Annotated[int, Field(ge=0)] = 0
    model_tokens_used: Annotated[int, Field(ge=0)] = 0
    model_tokens_unknown: Annotated[int, Field(ge=0)] = 0
    plan_revisions: Annotated[int, Field(ge=0)] = 0
    decision_rounds: Annotated[int, Field(ge=0)] = 0
    evidence_reads: Annotated[int, Field(ge=0)] = 0
    analysis_executions: Annotated[int, Field(ge=0)] = 0
    logical_steps: list[Identifier] = Field(default_factory=list)


class Attempt(Record):
    id: Identifier = Field(default_factory=lambda: new_id("attempt"))
    step_id: Identifier
    logical_id: Identifier
    number: Annotated[int, Field(ge=1)]
    tool: str
    state: Literal[
        "intent", "running", "completed", "failed", "cancelled", "timed_out", "unknown",
        "not_started",
    ] = "intent"
    geometry_artifact_id: Identifier
    input_fingerprint: str
    directory: str
    created_at: datetime = Field(default_factory=utc_now)
    finished_at: datetime | None = None
    execution_handle: dict[str, Any] | None = None
    started: bool = False
    result_id: Identifier | None = None
    elapsed_seconds: Annotated[float, Field(ge=0)] = 0
    cpu_seconds: Annotated[float, Field(ge=0)] = 0
    request_version: int | None = None
    plan_version: int | None = None
    permission_version: int | None = None
    frozen_step: Step | None = None
    consumption: dict[str, Any] = Field(default_factory=dict)


class ToolCall(Record):
    id: Identifier = Field(default_factory=lambda: new_id("call"))
    tool: str
    parameters: dict[str, Any]
    step_id: Identifier | None = None
    state: Literal["reserved", "completed", "failed", "unknown"] = "reserved"
    result_id: Identifier | None = None
    request_version: int
    plan_version: int | None = None
    created_at: datetime = Field(default_factory=utc_now)
    frozen_step: Step | None = None
    consumption: dict[str, Any] = Field(default_factory=dict)


class Proposal(Record):
    action: Literal["clarify", "initial_plan", "revise_plan", "call_tool", "stop"]
    request_version: int
    plan_version: int | None = None
    permission_version: int
    control_generation: int
    related_results: list[Identifier] = Field(default_factory=list)
    reason: Annotated[str, Field(max_length=2000)]
    parameters: dict[str, Any] = Field(default_factory=dict)


class Run(Record):
    id: Identifier = Field(default_factory=lambda: new_id("run"))
    request_id: Identifier
    request_version: int
    plan_id: Identifier | None = None
    plan_version: int | None = None
    permission: PermissionSnapshot = Field(default_factory=PermissionSnapshot)
    budget: BudgetLimits = Field(default_factory=BudgetLimits)
    usage: BudgetUsage = Field(default_factory=BudgetUsage)
    attempts: list[Attempt] = Field(default_factory=list)
    state: Literal[
        "ready", "running", "paused", "cancelled", "completed", "failed", "unknown",
        "budget_exhausted",
        "waiting_user",
    ] = "ready"
    goal_status: dict[str, Literal["satisfied", "partial", "unsatisfied", "insufficient_evidence"]] = (
        Field(default_factory=dict)
    )
    delivery_status: Literal["pending", "complete", "partial", "failed"] = "pending"
    created_at: datetime = Field(default_factory=utc_now)
    deadline: datetime = Field(default_factory=lambda: utc_now() + timedelta(seconds=1800))
    result_ids: list[Identifier] = Field(default_factory=list)
    diagnostics: list[dict[str, Any]] = Field(default_factory=list)
    calls: list[ToolCall] = Field(default_factory=list)
    model_records: list[dict[str, Any]] = Field(default_factory=list)
    selected_results: dict[str, Identifier] = Field(default_factory=dict)
    goal_evidence: dict[Identifier, EvidenceRef] = Field(default_factory=dict)
    initial_science_steps: list[Identifier] | None = None
    control_generation: int = 0
    processed_messages: list[Identifier] = Field(default_factory=list)
    processed_feedback: list[Identifier] = Field(default_factory=list)
    decisions: list[dict[str, Any]] = Field(default_factory=list)
    applied_decisions: list[Identifier] = Field(default_factory=list)
    agent_enabled: bool = False
    batch_category: Literal["formal", "development"] | None = None


class Check(Record):
    name: str
    status: CheckStatus
    detail: str = ""
    source: dict[str, Any] = Field(default_factory=dict)
    rule_version: str = LEGACY_CHECK_VERSION


class QualifiedOutput(Record):
    value: float | None = None
    unit: str | None = None
    artifact_id: Identifier | None = None
    checks: Annotated[list[Check], Field(min_length=1)]
    source: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def checked(self) -> QualifiedOutput:
        if any(check.status != "passed" for check in self.checks):
            raise ValueError("qualified outputs require every applicable check to pass")
        if self.value is None and self.artifact_id is None:
            raise ValueError("qualified output has no value or artifact")
        if self.value is not None and self.unit is None:
            raise ValueError("scientific numeric output requires an explicit unit")
        return self


class Result(Record):
    id: Identifier = Field(default_factory=lambda: new_id("result"))
    run_id: Identifier
    step_id: Identifier | None = None
    attempt_id: Identifier | None = None
    call_id: Identifier | None = None
    supersedes_result_id: Identifier | None = None
    operation_status: Literal["completed", "failed", "cancelled", "timed_out", "unknown"]
    checks: dict[str, list[Check]] = Field(default_factory=dict)
    qualified_outputs: dict[Port, QualifiedOutput] = Field(default_factory=dict)
    observations: dict[str, Any] = Field(default_factory=dict)
    diagnostics: list[dict[str, Any]] = Field(default_factory=list)
    artifact_ids: list[Identifier] = Field(default_factory=list)
    source: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=utc_now)


class Artifact(Record):
    id: Identifier = Field(default_factory=lambda: new_id("artifact"))
    path: str
    sha256: Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]
    size: Annotated[int, Field(ge=0)]
    role: str
    run_id: Identifier | None = None
    attempt_id: Identifier | None = None
    source: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=utc_now)


def fingerprint(value: BaseModel | dict[str, Any]) -> str:
    payload = value.model_dump(mode="json") if isinstance(value, BaseModel) else value
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
    ).hexdigest()
