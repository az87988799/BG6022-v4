"""The seven persisted objects and their small, typed local records.

Scientific requests can describe general conditions and code-backed work.
Representable parameters do not themselves grant execution or scientific qualification.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Annotated, Any, Literal
from uuid import uuid4

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, JsonValue, model_validator

from orca_agent.versions import LEGACY_CHECK_VERSION

Identifier = Annotated[str, Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]{0,95}$")]
Port = Annotated[str, Field(pattern=r"^[A-Za-z][A-Za-z0-9_]{0,63}$")]
CheckStatus = Literal["passed", "failed", "unverified", "not_applicable"]
Digest = Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]
ScientificLabel = Annotated[str, Field(strict=True, min_length=1, max_length=256,
                                      pattern=r"^[^\s\x00-\x1f\x7f](?:[^\x00-\x1f\x7f]*[^\s\x00-\x1f\x7f])?$")]
RuleVersion = Annotated[str, Field(min_length=3, max_length=96,
                                   pattern=r"^[A-Za-z][A-Za-z0-9_-]*-[0-9]+$")]
ConditionSource = Literal[
    "explicit", "default", "inherited", "inferred", "auto_selected", "unknown", "not_applicable",
]


def require_electronic_integer(value: Any) -> int:
    """Check the incoming scalar before Pydantic can coerce it or compare Literals."""
    if type(value) is not int:
        raise ValueError("charge/multiplicity require exact integer values")
    return value


ElectronicInteger = Annotated[int, BeforeValidator(require_electronic_integer)]


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid4().hex}"


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class Record(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class CalculationParameters(Record):
    method: ScientificLabel = "HF"
    basis: ScientificLabel = "STO-3G"
    charge: ElectronicInteger = 0
    multiplicity: Annotated[ElectronicInteger, Field(ge=1)] = 1
    cores: Annotated[int, Field(strict=True, ge=1)] = 4
    memory_mb: Annotated[int, Field(strict=True, ge=1)] = 1024
    maxcore_mb: Annotated[int, Field(strict=True, ge=1)] = 192
    scf_maxiter: Annotated[int, Field(strict=True, ge=1)] = 100
    opt_maxiter: Annotated[int, Field(strict=True, ge=1)] = 100
    timeout_seconds: Annotated[float, Field(gt=0)] = 300

    @model_validator(mode="after")
    def within_memory(self) -> CalculationParameters:
        if self.cores * self.maxcore_mb > self.memory_mb:
            raise ValueError("aggregate MaxCore exceeds the requested total memory")
        return self


class CodeRef(Record):
    """An immutable code/input Artifact binding; not permission to execute it."""

    artifact_id: Identifier
    sha256: Digest
    language: Literal["python", "orca"] = "python"
    version: Annotated[int, Field(strict=True, ge=1)] = 1


class InputManifestEntry(Record):
    artifact_id: Identifier
    sha256: Digest
    target_path: Annotated[str, Field(strict=True, min_length=1, max_length=240)]
    purpose: ScientificLabel

    @model_validator(mode="after")
    def relative_target(self):
        # This is a task-local copy name, never an arbitrary host filesystem path.
        parts = self.target_path.split("/")
        if (any(char in self.target_path for char in '\\:<>"|?*')
                or any(ord(char) < 32 or ord(char) == 127 for char in self.target_path)
                or PurePosixPath(self.target_path).is_absolute()
                or any(part in {"", ".", ".."} or part.endswith((" ", "."))
                       or PureWindowsPath(part).is_reserved() for part in parts)):
            raise ValueError("manifest target must be an unambiguous relative task path")
        return self


class InputManifest(Record):
    """Versioned, hash-pinned input copies and their declared use within a Step."""

    version: Literal[1] = 1
    entries: list[InputManifestEntry] = Field(default_factory=list)

    @model_validator(mode="after")
    def unique_targets(self):
        targets = [entry.target_path.casefold() for entry in self.entries]
        if len(set(targets)) != len(targets):
            raise ValueError("manifest target paths collide")
        for target in targets:
            if any(other.startswith(target + "/") for other in targets if other != target):
                raise ValueError("manifest target cannot be both a file and a directory")
        artifacts = {}
        for entry in self.entries:
            if artifacts.setdefault(entry.artifact_id, entry.sha256) != entry.sha256:
                raise ValueError("one manifest Artifact cannot have different hashes")
        return self


class CodeExecutionParameters(Record):
    code: CodeRef
    inputs: InputManifest = Field(default_factory=InputManifest)
    observation_names: list[Port] = Field(default_factory=list)
    conditions: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def python_code(self):
        if self.code.language != "python":
            raise ValueError("Python execution requires a Python code Artifact")
        if any(entry.artifact_id == self.code.artifact_id and entry.sha256 != self.code.sha256
               for entry in self.inputs.entries):
            raise ValueError("code and input manifest disagree on the Artifact hash")
        if len(set(self.observation_names)) != len(self.observation_names):
            raise ValueError("observation names must be unique")
        return self


class NativeOrcaParameters(Record):
    input: CodeRef
    inputs: InputManifest = Field(default_factory=InputManifest)
    observation_names: list[Port] = Field(default_factory=list)
    conditions: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def native_input(self):
        if self.input.language != "orca":
            raise ValueError("native ORCA execution requires an ORCA input Artifact")
        if any(entry.artifact_id == self.input.artifact_id and entry.sha256 != self.input.sha256
               for entry in self.inputs.entries):
            raise ValueError("native input and manifest disagree on the Artifact hash")
        if len(set(self.observation_names)) != len(self.observation_names):
            raise ValueError("observation names must be unique")
        return self


class InternalCall(Record):
    """Run-owned receipt data for a managed internal call, without a new lifecycle."""

    request_key: Identifier
    input_fingerprint: Digest
    tool: Literal["orca.run"] = "orca.run"
    code: CodeRef | None = None
    inputs: InputManifest = Field(default_factory=InputManifest)
    conditions: dict[str, Any] = Field(default_factory=dict)
    actual_conditions: dict[str, Any] = Field(default_factory=dict)
    state: Literal["reserved", "running", "completed", "failed", "cancelled", "timed_out", "unknown"]
    attempt_id: Identifier | None = None
    parent_attempt_id: Identifier | None = None
    work_units: Annotated[int, Field(strict=True, ge=1)] = 1
    started: bool = False
    execution_handle: dict[str, Any] | None = None
    resource_usage: dict[str, Any] = Field(default_factory=dict)
    result_id: Identifier | None = None
    artifact_ids: list[Identifier] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=utc_now)
    finished_at: datetime | None = None


class InputRef(Record):
    artifact_id: Identifier | None = None
    producer_step_id: Identifier | None = None
    port: Literal["optimized_geometry", "prepared_geometry"] | None = None

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
    minimum_check_version: RuleVersion = LEGACY_CHECK_VERSION
    evidence_contract_version: Literal["legacy-1", "generic-observation-1"] = "legacy-1"
    verification: Literal["qualified", "reported_with_limitations"] = "qualified"
    quantity: ScientificLabel | None = None
    original_text: str = ""
    identity: dict[str, Any] = Field(default_factory=dict)
    text_evidence: dict[str, Any] = Field(default_factory=dict)
    system_ids: list[Identifier] = Field(default_factory=list)
    conditions: dict[str, Any] = Field(default_factory=dict)
    minimum_evidence: list[str] = Field(default_factory=list)
    minimum_evidence_source: dict[str, ConditionSource] = Field(default_factory=dict)
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
    identity: dict[str, Any] = Field(default_factory=dict)
    geometry_source: Literal["registered", "prepare"] = "registered"


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
    charge: ElectronicInteger | None = 0
    multiplicity: Annotated[ElectronicInteger, Field(ge=1)] | None = 1
    method: ScientificLabel | None = "HF"
    basis: ScientificLabel | None = "STO-3G"
    conditions_source: dict[str, ConditionSource] = Field(
        default_factory=lambda: {
            "charge": "explicit", "multiplicity": "explicit", "method": "explicit",
            "basis": "explicit", "geometry": "explicit",
        }
    )
    goals: Annotated[list[Goal], Field(min_length=1)]
    systems: list[SystemInput] = Field(default_factory=list)
    messages: list[dict[str, Any]] = Field(default_factory=list)
    unresolved: list[str] = Field(default_factory=list)
    conditions: dict[str, Any] = Field(default_factory=dict)
    normalization_status: Literal["structured", "pending", "clarification", "normalized"] = "structured"
    condition_evidence: dict[str, Any] = Field(default_factory=dict)
    semantic_defaults: dict[str, Any] = Field(default_factory=dict)
    automatic_selection: list[str] = Field(default_factory=list)
    selection_reasons: dict[str, str] = Field(default_factory=dict)

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
        tool = get_tool(self.tool)
        if ("execute_orca" in tool.effects and "native_input" not in tool.input_roles
                and self.geometry is None):
            raise ValueError("scientific tool requires geometry")
        if ("execute_orca" not in tool.effects or "native_input" in tool.input_roles) and self.geometry:
            raise ValueError("non-scientific tool cannot claim a scientific geometry binding")
        return self


class Plan(Record):
    id: Identifier = Field(default_factory=lambda: new_id("plan"))
    version: Annotated[int, Field(ge=1)] = 1
    request_id: Identifier
    request_version: Annotated[int, Field(ge=1)] = 1
    steps: Annotated[list[Step], Field(min_length=1)]
    goal_map: dict[str, OutputBinding]

    @model_validator(mode="after")
    def valid_graph(self) -> Plan:
        from orca_agent.tools.registry import declared_outputs, get_tool
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
                    if reference.port not in declared_outputs(steps[reference.producer_step_id]):
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
            if binding.step_id and binding.port not in declared_outputs(steps[binding.step_id]):
                raise ValueError("step does not produce the goal's port")
        return self

    def validate_request(self, request: Request) -> None:
        from orca_agent.applicability import validate_scientific_plan
        from orca_agent.planning import validate_goal_output_contract
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
            # Concrete historical optimized geometry can be outside this Plan.
            # Store validates its Artifact/Attempt lineage before activation.
        validate_goal_output_contract(request, self)
        validate_scientific_plan(request, self)


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
    # None means the Run configuration supplies the limit; zero denotes no CPU execution.
    max_cores: int | None = None
    max_memory_mb: int | None = None
    check_version: str = LEGACY_CHECK_VERSION
    implementation: str
    required_input_checks: dict[str, str] = Field(default_factory=dict)
    check_contract: dict[str, Any] = Field(default_factory=dict)
    preflight: str | None = None
    recovery: str | None = None
    usage_counter: Literal["identity_queries", "structure_preparations", "knowledge_queries"] | None = None


class PermissionSnapshot(Record):
    version: Annotated[int, Field(ge=1)] = 1
    scientific_execution: bool = False
    allowed_tools: list[str] = Field(
        default_factory=lambda: ["orca.sp", "orca.opt"]
    )
    max_cores: Annotated[int, Field(strict=True, ge=1)] = 4
    max_memory_mb: Annotated[int, Field(strict=True, ge=1)] = 1024
    artifact_ids: list[Identifier] = Field(default_factory=list)
    source_ids: list[Identifier] = Field(default_factory=list)
    result_ids: list[Identifier] = Field(default_factory=list)
    model_execution: bool = False
    code_execution: bool = False
    artifact_writes: bool = False
    allowed_repairs: dict[str, list[int]] = Field(default_factory=dict)
    allow_additional_science: bool = False
    external_identity_queries: bool = False
    geometry_preparation: bool = False

    @model_validator(mode="after")
    def registered_tools(self):
        from orca_agent.tools.registry import get_tool
        for name in self.allowed_tools:
            get_tool(name)
        return self


class BudgetLimits(Record):
    logical_steps: Annotated[int, Field(strict=True, ge=1)] = 12
    attempts_per_step: Annotated[int, Field(ge=1)] = 3
    orca_starts: Annotated[int, Field(ge=0)] = 4
    extra_orca_starts: Annotated[int, Field(ge=0)] = 3
    postprocess_starts: Annotated[int, Field(strict=True, ge=0)] = 0
    run_seconds: Annotated[float, Field(gt=0)] = 1800
    model_calls: Annotated[int, Field(ge=0)] = 0
    plan_revisions: Annotated[int, Field(ge=0)] = 0
    model_tokens: Annotated[int, Field(ge=0)] = 0
    input_tokens: Annotated[int, Field(ge=0)] = 0
    output_tokens: Annotated[int, Field(ge=0)] = 0
    decision_rounds: Annotated[int, Field(ge=0)] = 0
    evidence_reads: Annotated[int, Field(ge=0)] = 0
    analysis_executions: Annotated[int, Field(ge=0)] = 0
    corrections_per_proposal: Annotated[int, Field(ge=0)] = 0
    transport_retries: Annotated[int, Field(ge=0)] = 0
    identity_queries: Annotated[int, Field(strict=True, ge=0)] = 0
    structure_preparations: Annotated[int, Field(strict=True, ge=0)] = 0
    knowledge_queries: Annotated[int, Field(strict=True, ge=0)] = 0

    internal_work_units: Annotated[int, Field(strict=True, ge=0)] = 0
    code_retries: Annotated[int, Field(strict=True, ge=0)] = 0
    artifact_count: Annotated[int, Field(strict=True, ge=0)] = 200
    artifact_bytes: Annotated[int, Field(strict=True, ge=0)] = 256 * 1024 * 1024
    artifact_total_bytes: Annotated[int, Field(strict=True, ge=0)] = 256 * 1024 * 1024


class ResourceLimits(Record):
    """One configured environment envelope, independent of scientific parameters."""

    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)
    max_cores: Annotated[int, Field(strict=True, ge=1)] = 4
    total_memory_mb: Annotated[int, Field(strict=True, ge=1)] = 1024
    maxcore_mb: Annotated[int, Field(strict=True, ge=1)] = 192
    environment_concurrency: Annotated[int, Field(strict=True, ge=1)] = 1
    job_timeout_seconds: Annotated[float, Field(gt=0)] = 900
    script_timeout_seconds: Annotated[float, Field(gt=0)] = 300
    collection_timeout_seconds: Annotated[float, Field(gt=0)] = 30


class ModelLimits(Record):
    """Transport/context limits; these are distinct from cumulative token budgets."""

    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)
    input_tokens: Annotated[int, Field(strict=True, ge=1)] = 24000
    output_tokens: Annotated[int, Field(strict=True, ge=1)] = 2000
    timeout_seconds: Annotated[float, Field(gt=0)] = 60
    response_bytes: Annotated[int, Field(strict=True, ge=1)] = 128 * 1024
    proposal_bytes: Annotated[int, Field(strict=True, ge=1)] = 32 * 1024
    messages: Annotated[int, Field(strict=True, ge=1)] = 64

    @model_validator(mode="after")
    def response_capacity(self):
        if self.proposal_bytes > self.response_bytes:
            raise ValueError("proposal byte limit exceeds response byte limit")
        return self


class ExecutionConfiguration(Record):
    """Trusted host selection, frozen per Run; never accepted from task code."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    backend: Literal["windows-lpac", "linux-container"] = "windows-lpac"
    image_id: Annotated[str, Field(strict=True, pattern=r"^sha256:[a-f0-9]{64}$")] | None = None
    manifest_sha256: Digest | None = None
    docker_context: Annotated[str, Field(strict=True,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")] | None = None
    docker_path: Path | None = None

    @model_validator(mode="after")
    def explicit_backend_identity(self):
        fields = (self.image_id, self.manifest_sha256, self.docker_context, self.docker_path)
        if self.backend == "windows-lpac":
            if any(value is not None for value in fields):
                raise ValueError("Windows execution cannot carry Linux container configuration")
        elif any(value is None for value in fields):
            raise ValueError("Linux container execution requires an immutable image, manifest, context and Docker path")
        if self.docker_path is not None:
            value = str(self.docker_path)
            windows = PureWindowsPath(value)
            if (not (PurePosixPath(value).is_absolute() or windows.is_absolute())
                    or value.startswith(("\\\\", "//"))
                    or any(ord(char) < 32 for char in value)
                    or ".." in windows.parts or ".." in PurePosixPath(value).parts):
                raise ValueError("Docker executable must be an absolute local trusted host path")
        return self


class RunConfiguration(Record):
    """Credential-free configuration captured at Run creation, never on resume."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    version: Literal[1, 2] = 2
    resources: ResourceLimits = Field(default_factory=ResourceLimits)
    model: ModelLimits = Field(default_factory=ModelLimits)
    model_profile: Literal["disabled", "thinking_low"] = "disabled"
    execution: ExecutionConfiguration = Field(default_factory=ExecutionConfiguration)

    @model_validator(mode="before")
    @classmethod
    def explicit_version_two_backend(cls, value):
        if isinstance(value, dict) and value.get("version") == 2 and "execution" not in value:
            raise ValueError("version 2 Run configuration requires an explicit execution snapshot")
        return value

    @model_validator(mode="after")
    def historical_backend(self):
        if self.version == 1 and self.execution.backend != "windows-lpac":
            raise ValueError("version 1 Run configuration retains the Windows execution backend")
        return self


class BudgetUsage(Record):
    internal_work_units: Annotated[int, Field(strict=True, ge=0)] = 0
    code_retries: Annotated[int, Field(strict=True, ge=0)] = 0
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
    identity_queries: Annotated[int, Field(strict=True, ge=0)] = 0
    structure_preparations: Annotated[int, Field(strict=True, ge=0)] = 0
    knowledge_queries: Annotated[int, Field(strict=True, ge=0)] = 0


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
    # Historical attempts are ORCA attempts. General execution records the kind
    # explicitly so starting Python never charges an actual ORCA launch.
    execution_kind: Literal["orca", "python"] = "orca"
    geometry_artifact_id: Identifier | None = None
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
    control_generation: int | None = None
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
    control_generation: int | None = None
    created_at: datetime = Field(default_factory=utc_now)
    frozen_step: Step | None = None
    consumption: dict[str, Any] = Field(default_factory=dict)


class Proposal(Record):
    action: Literal["clarify", "initial_plan", "revise_plan", "call_tool", "stop", "normalize_request"]
    request_version: int
    plan_version: int | None = None
    permission_version: int
    control_generation: int
    related_results: list[Identifier] = Field(default_factory=list)
    reason: Annotated[str, Field(max_length=2000)]
    parameters: dict[str, Any] = Field(default_factory=dict)


class TerminalReceipt(Record):
    """Local Run evidence, not an independent domain lifecycle."""

    decision_id: Identifier
    contract_version: Literal["terminal-delivery-1"]
    basis: dict[str, Any]
    snapshot_fingerprint: str
    snapshot: dict[str, Any]
    explanation: dict[str, Any]
    contract_status: Literal["passed"] = "passed"
    terminal_state: Literal["completed", "failed"]
    report_status: Literal["pending", "rendered", "failed"] = "pending"
    report_version: str = "terminal-report-1"
    report_path: str | None = None
    report_sha256: str | None = None
    report_error: str | None = None


class ReportReceipt(Record):
    """Derived non-stop report evidence; never an accepted model decision."""

    publication_id: Identifier
    basis: dict[str, Any]
    snapshot_fingerprint: str
    snapshot: dict[str, Any]
    report_status: Literal["pending", "rendered", "failed"] = "pending"
    report_version: str = "terminal-report-1"
    report_path: str | None = None
    report_sha256: str | None = None
    report_error: str | None = None


class Run(Record):
    # Missing historical snapshots stay absent; loading does not invent past settings.
    configuration: RunConfiguration | None = None
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
    internal_calls: list[InternalCall] = Field(default_factory=list)
    model_records: list[dict[str, Any]] = Field(default_factory=list)
    selected_results: dict[str, Identifier] = Field(default_factory=dict)
    goal_evidence: dict[Identifier, EvidenceRef] = Field(default_factory=dict)
    initial_science_steps: list[Identifier] | None = None
    control_generation: int = 0
    processed_messages: list[Identifier] = Field(default_factory=list)
    processed_feedback: list[Identifier] = Field(default_factory=list)
    decisions: list[dict[str, Any]] = Field(default_factory=list)
    applied_decisions: list[Identifier] = Field(default_factory=list)
    terminal_deliveries: list[TerminalReceipt] = Field(default_factory=list)
    fallback_report_receipts: list[ReportReceipt] = Field(default_factory=list)
    reopened_terminal_ids: list[Identifier] = Field(default_factory=list)
    agent_enabled: bool = False
    batch_category: Literal["formal", "development"] | None = None
    input_bindings: dict[Identifier, dict[str, Any]] = Field(default_factory=dict)
    science_baseline_policy: Literal["legacy", "first_science_plan"] = "legacy"


class Check(Record):
    name: str
    status: CheckStatus
    detail: str = ""
    source: dict[str, Any] = Field(default_factory=dict)
    rule_version: str = LEGACY_CHECK_VERSION


class ObservationSource(Record):
    """A source identity and the known location, without inventing missing metadata."""

    artifact_id: Identifier
    sha256: Digest
    location: str | None = None
    attempt_id: Identifier | None = None
    internal_request_key: Identifier | None = None


class Observation(Record):
    """General reported data, never an implicit QualifiedOutput or success claim."""

    name: ScientificLabel
    value: JsonValue = None
    unit: str | None = None
    actual_conditions: dict[str, Any] = Field(default_factory=dict)
    sources: list[ObservationSource] = Field(default_factory=list)
    code_sha256: Digest | None = None
    checks: list[Check] = Field(default_factory=list)
    validation_status: Literal["unverified", "partially_checked", "failed"] = "unverified"
    limitations: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def finite_json_value(self):
        json.dumps(self.value, allow_nan=False)
        return self


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
    observation_records: list[Observation] = Field(default_factory=list)
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


def result_fingerprint_payload(result: Result, *, exclude: set[str] | None = None) -> dict[str, Any]:
    """Preserve historical evidence identities without discarding old default fields."""
    payload = result.model_dump(mode="json", exclude=exclude)
    if not result.observation_records:
        # Existing consumption receipts bind the historical Result serialization.
        # An absent/empty additive observation list has no new evidence to hash;
        # keep all pre-existing defaults and retain every nonempty new observation.
        payload.pop("observation_records", None)
    return payload


def fingerprint(value: BaseModel | dict[str, Any]) -> str:
    payload = (result_fingerprint_payload(value) if isinstance(value, Result) else
               value.model_dump(mode="json") if isinstance(value, BaseModel) else value)
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
    ).hexdigest()
