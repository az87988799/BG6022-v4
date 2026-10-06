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

Identifier = Annotated[str, Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]{0,95}$")]
Port = Literal["energy", "optimized_geometry"]
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
    minimum_check_version: Literal["orca-hf-1"] = "orca-hf-1"


class OutputBinding(Record):
    step_id: Identifier
    port: Port


class Request(Record):
    id: Identifier = Field(default_factory=lambda: new_id("request"))
    version: Annotated[int, Field(ge=1)] = 1
    original_text: str = "Structured local calculation"
    geometry_artifact_id: Identifier
    charge: Literal[0] = 0
    multiplicity: Literal[1] = 1
    method: Literal["HF"] = "HF"
    basis: Literal["STO-3G"] = "STO-3G"
    conditions_source: dict[str, Literal["explicit", "default", "inherited", "inferred"]] = Field(
        default_factory=lambda: {
            "charge": "explicit", "multiplicity": "explicit", "method": "explicit",
            "basis": "explicit", "geometry": "explicit",
        }
    )
    goals: Annotated[list[Goal], Field(min_length=1)]

    @model_validator(mode="after")
    def unique_goals(self) -> Request:
        if len({goal.id for goal in self.goals}) != len(self.goals):
            raise ValueError("goal identities must be unique")
        if not any(goal.required for goal in self.goals):
            raise ValueError("a scientific request requires at least one mandatory goal")
        return self


class Step(Record):
    id: Identifier
    logical_id: Identifier
    tool: Literal["orca.sp", "orca.opt"]
    parameters: CalculationParameters = Field(default_factory=CalculationParameters)
    geometry: InputRef
    depends_on: list[Identifier] = Field(default_factory=list)


class Plan(Record):
    id: Identifier = Field(default_factory=lambda: new_id("plan"))
    version: Annotated[int, Field(ge=1)] = 1
    request_id: Identifier
    request_version: Annotated[int, Field(ge=1)] = 1
    steps: Annotated[list[Step], Field(min_length=1, max_length=4)]
    goal_map: dict[str, OutputBinding]

    @model_validator(mode="after")
    def valid_graph(self) -> Plan:
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
            producer = step.geometry.producer_step_id
            if producer:
                if producer not in step.depends_on:
                    raise ValueError("future geometry producer must be an explicit dependency")
                if steps[producer].tool != "orca.opt":
                    raise ValueError("producer does not declare optimized_geometry")
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
            if binding.step_id not in steps:
                raise ValueError("goal binding references an unknown step")
            if binding.port == "optimized_geometry" and steps[binding.step_id].tool != "orca.opt":
                raise ValueError("step does not produce the goal's port")
        return self

    def validate_request(self, request: Request) -> None:
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
            for name in ("charge", "multiplicity", "method", "basis"):
                if getattr(step.parameters, name) != getattr(request, name):
                    raise ValueError(f"step changes request condition: {name}")
            if step.geometry.artifact_id and step.geometry.artifact_id != request.geometry_artifact_id:
                raise ValueError("direct geometry must bind the request's initial geometry")


class Tool(Record):
    name: Literal["orca.sp", "orca.opt", "evidence.list", "evidence.text", "evidence.field"]
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
    check_version: str = "orca-hf-1"
    implementation: str


class PermissionSnapshot(Record):
    version: Annotated[int, Field(ge=1)] = 1
    scientific_execution: bool = False
    allowed_tools: list[Literal["orca.sp", "orca.opt"]] = Field(
        default_factory=lambda: ["orca.sp", "orca.opt"]
    )
    max_cores: Annotated[int, Field(ge=1, le=4)] = 4
    max_memory_mb: Annotated[int, Field(ge=256, le=1024)] = 1024
    artifact_ids: list[Identifier] = Field(default_factory=list)


class BudgetLimits(Record):
    attempts_per_step: Annotated[int, Field(ge=1, le=3)] = 3
    orca_starts: Annotated[int, Field(ge=1, le=4)] = 4
    extra_orca_starts: Annotated[int, Field(ge=0, le=3)] = 3
    postprocess_starts: Literal[0] = 0
    run_seconds: Annotated[float, Field(gt=0, le=1800)] = 1800
    model_calls: Literal[0] = 0
    plan_revisions: Literal[0] = 0


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


class Attempt(Record):
    id: Identifier = Field(default_factory=lambda: new_id("attempt"))
    step_id: Identifier
    logical_id: Identifier
    number: Annotated[int, Field(ge=1)]
    tool: Literal["orca.sp", "orca.opt"]
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


class Run(Record):
    id: Identifier = Field(default_factory=lambda: new_id("run"))
    request_id: Identifier
    request_version: int
    plan_id: Identifier
    plan_version: int
    permission: PermissionSnapshot = Field(default_factory=PermissionSnapshot)
    budget: BudgetLimits = Field(default_factory=BudgetLimits)
    usage: BudgetUsage = Field(default_factory=BudgetUsage)
    attempts: list[Attempt] = Field(default_factory=list)
    state: Literal[
        "ready", "running", "paused", "cancelled", "completed", "failed", "unknown",
        "budget_exhausted",
    ] = "ready"
    goal_status: dict[str, Literal["satisfied", "partial", "unsatisfied", "insufficient_evidence"]] = (
        Field(default_factory=dict)
    )
    delivery_status: Literal["pending", "complete", "partial", "failed"] = "pending"
    created_at: datetime = Field(default_factory=utc_now)
    deadline: datetime = Field(default_factory=lambda: utc_now() + timedelta(seconds=1800))
    result_ids: list[Identifier] = Field(default_factory=list)
    diagnostics: list[dict[str, Any]] = Field(default_factory=list)


class Check(Record):
    name: str
    status: CheckStatus
    detail: str = ""
    source: dict[str, Any] = Field(default_factory=dict)
    rule_version: str = "orca-hf-1"


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
    step_id: Identifier
    attempt_id: Identifier
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
