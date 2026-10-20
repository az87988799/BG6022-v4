"""Translate symbolic model intentions into program-owned Plan identities."""

import copy
import math
import re
from typing import Annotated, Any, Literal

from pydantic import ConfigDict, Field, ValidationError, field_validator, model_validator

from orca_agent.models import (
    ArtifactCodeSelection,
    CodeRef,
    ContractValidationError,
    EvidenceRef,
    Identifier,
    InputManifest,
    InputRef,
    OutputBinding,
    Plan,
    Port,
    ProducerFileRef,
    Record,
    Step,
    fingerprint,
    new_id,
)
from orca_agent.tools.registry import get_tool, validate_parameters

_CALL_TOOL_FORMS = ({"step_id": str}, {"tool": str, "parameters": dict})


ShortRef = Annotated[str, Field(strict=True, min_length=1, max_length=64)]


def _bounded_reason(value):
    """Bound audit prose; the immutable model response retains the original."""
    if isinstance(value, str) and len(value) > 1000:
        marker = " [reason shortened; full text in model response]"
        return value[:1000 - len(marker)] + marker
    return value


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
    bound_reason = field_validator("reason", mode="before")(_bounded_reason)


class ContractStopParameters(StopParameters):
    """The schema advertised by new requests; legacy parameters stay readable."""

    delivery: TerminalExplanation


class ClarifyParameters(Record):
    questions: list[Annotated[str, Field(strict=True, min_length=1, max_length=1000)]] = Field(min_length=1, max_length=5)
    unresolved: list[Annotated[str, Field(strict=True, min_length=1, max_length=1000)]] = Field(min_length=1, max_length=5)


class DecisionIntent(Record):
    """Transient model choice; all execution identity comes from its frozen send."""

    action: str = Field(strict=True)
    parameters: dict[str, Any]
    reason: str = Field(strict=True, max_length=1000)
    bound_reason = field_validator("reason", mode="before")(_bounded_reason)

    @model_validator(mode="before")
    @classmethod
    def normalize_envelope(cls, values):
        """Normalize only unambiguous transport forms, never execution content."""
        if not isinstance(values, dict):
            return values
        values = dict(values)
        if values.get("type") == "json_object":
            values.pop("type")
        if (values.get("schema_version") == "request-semantics-3"
                and values.get("kind") in {"normalize", "amend", "replace_goals", "clarify", "continue", "status"}
                and "action" not in values and "parameters" not in values):
            # The semantic schema still validates every field, message and
            # permission. This merely restores the missing action envelope;
            # the immutable provider response retains the original shape.
            return {"action": "normalize_request", "parameters": values, "reason": ""}
        return values


def action_parameter_schema(action, *, immediate=True, terminal_required=False):
    if action == "stop":
        return (ContractStopParameters if terminal_required else StopParameters).model_json_schema()
    if action == "clarify":
        return ClarifyParameters.model_json_schema()
    if action == "call_tool":
        return call_tool_parameters_schema(immediate=immediate)
    if action in {"initial_plan", "revise_plan"}:
        return proposed_plan_schema()
    raise ValueError("action parameters are owned by their specialized contract")


def proposed_plan_schema(*, code_repair=True):
    """Project the single proposal contract for the Run's authorized surface."""
    schema = ProposedPlan.model_json_schema()
    if not code_repair:
        schema["properties"]["steps"]["items"] = {"$ref": "#/$defs/ProposedStep"}
        _prune_definitions(schema)
    return schema


def plan_structure_schema():
    """Bounded structural projection of the one production Plan parameter type.

    Step fields and EvidenceRef contents are intentionally not expanded here;
    the full schema and runtime validation remain available and unchanged.
    """
    schema = proposed_plan_schema()
    definitions = schema["$defs"]
    schema["properties"]["steps"]["items"] = {"type": "object"}
    definitions[EvidenceRef.__name__] = {"type": definitions[EvidenceRef.__name__]["type"]}
    schema["description"] = (
        "Structural projection only: Step fields and EvidenceRef contents are validated separately.")

    _prune_definitions(schema)
    return schema


def _prune_definitions(schema):
    # Dropped expansion layers can leave unused definitions. Retain the exact
    # transitive closure, never a second copy of the target validation rules.
    definitions = schema["$defs"]
    needed = set()

    def visit(node):
        if isinstance(node, dict):
            ref = node.get("$ref")
            if isinstance(ref, str) and ref.startswith("#/$defs/"):
                name = ref.removeprefix("#/$defs/")
                if name not in needed:
                    needed.add(name)
                    visit(definitions[name])
            for key, value in node.items():
                if key != "$defs":
                    visit(value)
        elif isinstance(node, list):
            for value in node:
                visit(value)

    visit(schema)
    schema["$defs"] = {name: definition for name, definition in definitions.items() if name in needed}


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
            return model.model_validate(values).model_dump(exclude_unset=True)
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
    elif action in {"initial_plan", "revise_plan"}:
        _validate_plan_parameters(values)


def call_tool_parameter_shapes(*, immediate=True):
    """Exact field sets shared by schema, prose and correction diagnostics."""
    return [list(form) for form in (_CALL_TOOL_FORMS if immediate else _CALL_TOOL_FORMS[:1])]


def call_tool_instruction(*, immediate=True):
    shapes = ["{" + ",".join(keys) + "}" for keys in call_tool_parameter_shapes(immediate=immediate)]
    text = f"call_tool Plan Step={shapes[0]} only; no inline params; match reason."
    if immediate:
        text += f" Or {shapes[1]} for an immediate reader; tool=catalog.name, never effects."
    return text


def call_tool_parameters_schema(*, immediate=True, step_ids=None, tool_names=None):
    """One structural contract; scientific parameters remain the Tool's job."""
    types = {str: "string", dict: "object"}
    forms = _CALL_TOOL_FORMS if immediate else _CALL_TOOL_FORMS[:1]
    forms = [form for form in forms if not (
        "step_id" in form and step_ids == [] or "tool" in form and tool_names == [])]
    if not forms:
        return {"not": {}}
    alternatives = [{"required": list(form), "maxProperties": len(form)} for form in forms]
    properties = {field: {"type": types[kind]} for form in forms for field, kind in form.items()}
    for field, choices in (("step_id", step_ids), ("tool", tool_names)):
        if choices is not None and field in properties:
            properties[field]["enum"] = list(dict.fromkeys(choices))
    return {"type": "object", "properties": properties,
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


def validation_error_details(exc):
    """Expose trusted contract diagnostics, never arbitrary exception/input text."""
    details = []
    for error in exc.errors(include_input=False, include_url=False):
        item = {"type": error["type"], "loc": list(error["loc"])}
        context = error.get("ctx", {})
        expected = context.get("expected")
        if error["type"] in {"literal_error", "enum"} and isinstance(expected, str):
            # Pydantic's declared alternatives, not the submitted value or an
            # arbitrary validator message, make a literal rejection correctable.
            item["expected"] = expected[:1024]
            if len(expected) > 1024:
                item["expected_truncated"] = True
        contract = context.get("error")
        if isinstance(contract, ContractValidationError):
            item["loc"].extend(contract.path)
            item["reason"] = str(contract)
            item.update(contract.declarations)
        bounds = {key: context[key] for key in ("ge", "gt", "le", "lt", "multiple_of", "min_length", "max_length")
                  if key in context and type(context[key]) in (int, float) and math.isfinite(context[key])}
        if bounds:
            item["constraints"] = bounds
        details.append(item)
    return details


def _schema_error(exc, *, path, tool=None, errors_details=None):
    def location(part):
        if isinstance(part, int):
            return part
        return part if re.fullmatch(r"[A-Za-z_][A-Za-z_0-9]{0,63}", str(part)) else "[field]"

    details = validation_error_details(exc) if errors_details is None else errors_details
    references = {"CodeRef", "ArtifactCodeSelection", "ProducerFileRef",
                  "InputManifestEntry", "ArtifactInputSelection", "ProducerInputEntry"}
    errors = []
    for error in details:
        loc = []
        for part in error["loc"]:
            # Pydantic's union class labels are not keys in the submitted JSON.
            branch = str(part).rstrip("]").rsplit(", ", 1)[-1]
            reference_position = loc and (loc[-1] in {"code", "input"}
                or len(loc) > 1 and loc[-2] == "entries" and isinstance(loc[-1], int))
            if reference_position and branch in references:
                continue
            loc.append(location(part))
        value = {**error, "loc": [*path, *loc]}
        if value not in errors:
            errors.append(value)
    detail = {"tool": tool} if tool else {}
    if tool:
        definition = get_tool(tool)
        # These roles consume pinned files, unlike declared future output
        # bindings. Explain the existing boundary without inventing a file or
        # changing any Goal on the model's behalf.
        fields = {field for role, field in (("code", "code"), ("native_input", "input"),
                                            ("input_manifest", "inputs"))
                  if role in definition.input_roles
                  and field in definition.parameter_schema.get("properties", {})}
        affected = sorted({item["loc"][len(path)] for item in errors
                           if len(item["loc"]) > len(path) and item["loc"][len(path)] in fields})
        if affected:
            detail["corrections"] = [{"path": [*path, field],
                "reason": "explicit_file_reference_required",
                "instruction": (
                    "Use a registered Artifact reference. Under workflow_policy=simplified-1, its known "
                    "hash/language may be omitted, or use an explicit producer_step_id/producer_key "
                    "with port (written_artifact or prepared_geometry) or exact file_key. For a file-writing "
                    "producer, file_key must equal its declared name. For raw execution files use file_key "
                    "with the exact archived filename; artifacts/observations are output collections, not file ports. Manifest entries "
                    "cannot turn an execution output collection into written_artifact. If the exact filename is unknown, "
                    "plan and execute the producer first, inspect Result.source.files, then add analysis; pending Goals "
                    "may remain gaps. Never guess a filename from a port. Manifest entries "
                    "also require target_path and purpose. The program validates dependencies, permissions "
                    "and qualifications and freezes actual hashes before execution. Choose one reference form; "
                    "union alternatives are not cumulative. Historical policies "
                    "still require concrete ID/hash references. Never guess hashes or weaken Goals.")}
                for field in affected]
    # Keep the same eight-error display bound while showing each affected
    # top-level parameter before repeating another parameter's union failures.
    groups = {}
    for error in errors:
        relative = error["loc"][len(path):]
        groups.setdefault(relative[0] if relative else None, []).append(error)
    displayed = []
    while len(displayed) < 8 and any(groups.values()):
        for group in groups.values():
            if group and len(displayed) < 8:
                displayed.append(group.pop(0))
    requirement = "Use the declared parameter schema at each listed path."
    reasons = list(dict.fromkeys(item["reason"] for item in displayed if "reason" in item))
    if reasons:
        requirement += " " + " ".join(reasons)
    return ProposalError(requirement, **detail, errors=displayed)


class ProposedStep(Record):
    key: str
    logical_key: str | None = None
    tool: str | None = None
    parameters: dict[str, Any] = Field(default_factory=dict)
    system_id: str | None = None
    geometry: dict[str, Any] | None = Field(default=None,
        description="Geometry-role Tools bind an Artifact or producer here. File-manifest Tools use "
        "parameters.inputs entries with explicit target_path and purpose; an identical geometry "
        "reference here is accepted only as a redundant alias of that manifest entry.")
    depends_on: list[str] = Field(default_factory=list)
    inputs: dict[str, dict[str, Any]] = Field(default_factory=dict)


class ProposedCodeRepair(Record):
    """Reuse a failed contract with explicit code and optional input-copy repair."""

    model_config = ConfigDict(json_schema_extra={"oneOf": [
        {"required": ["code"], "properties": {"code": {"type": "object"}, "input": {"type": "null"}}},
        {"required": ["input"], "properties": {"input": {"type": "object"}, "code": {"type": "null"}}},
    ]})

    key: ShortRef
    repair_of: Identifier
    code: CodeRef | ArtifactCodeSelection | ProducerFileRef | None = None
    input: CodeRef | ArtifactCodeSelection | ProducerFileRef | None = None
    inputs: InputManifest | None = None
    sdk_orca: bool | None = Field(default=None, strict=True)

    @model_validator(mode="after")
    def one_replacement(self):
        if (self.code is None) == (self.input is None):
            raise ValueError("repair requires exactly one code or native input replacement")
        return self


class FutureGoalTarget(Record):
    step_key: str
    port: Port


class EvidenceGoalTarget(Record):
    evidence: EvidenceRef
    port: Port


class GapGoalTarget(Record):
    gap: str
    port: Port


_GOAL_TARGET_TYPES = (FutureGoalTarget, EvidenceGoalTarget, GapGoalTarget)


def goal_target_shapes():
    """Disjoint closed records are the shared schema and correction contract."""
    return [list(model.model_fields) for model in _GOAL_TARGET_TYPES]


class ProposedPlan(Record):
    steps: list[ProposedStep | ProposedCodeRepair] = Field(min_length=1)
    # Keys are checked against this Request's IDs, not merely an identifier
    # regex; only the target shape is a context-independent schema contract.
    goal_map: dict[str, FutureGoalTarget | EvidenceGoalTarget | GapGoalTarget] = Field(default_factory=dict)


def _validate_plan_parameters(parameters):
    try:
        return ProposedPlan.model_validate(parameters)
    except ValidationError as exc:
        # A disjoint alternative must not add spurious missing fields to the
        # submitted shape's correction feedback, or consume its bounded slots.
        submitted = parameters.get("steps", []) if isinstance(parameters, dict) else []
        repair_indexes = {index for index, item in enumerate(submitted) if isinstance(item, dict)
                          and ("repair_of" in item or "code" in item)} if isinstance(submitted, list) else set()
        details = []
        for item in validation_error_details(exc):
            loc = list(item["loc"])
            loc = ["ProposedCodeRepair" if part == "function-after[one_replacement(), ProposedCodeRepair]"
                   else "ProducerFileRef" if part == "function-after[exact_selector(), ProducerFileRef]"
                   else part for part in loc]
            if len(loc) > 2 and loc[0] == "steps" and loc[2] in {"ProposedStep", "ProposedCodeRepair"}:
                expected = "ProposedCodeRepair" if loc[1] in repair_indexes else "ProposedStep"
                if loc[2] != expected:
                    continue
            if (len(loc) > 4 and loc[0] == "steps" and loc[2] == "ProposedCodeRepair"
                    and loc[3] in {"code", "input"} and loc[4] in {
                        "CodeRef", "ArtifactCodeSelection", "ProducerFileRef"}):
                value = submitted[loc[1]].get(loc[3], {})
                expected = ("ProducerFileRef" if isinstance(value, dict)
                            and ("producer_key" in value or "producer_step_id" in value)
                            else "CodeRef" if isinstance(value, dict) and "sha256" in value
                            else "ArtifactCodeSelection")
                if loc[4] != expected:
                    continue
                loc.pop(4)
            details.append({**item, "loc": loc})
        error = _schema_error(exc, path=["parameters"], errors_details=details)
        # Union branch names are implementation details, not JSON paths. Keep
        # nested EvidenceRef failures at their real field path as well.
        branch_names = {model.__name__ for model in _GOAL_TARGET_TYPES}
        errors = []
        for item in error.detail["errors"]:
            loc = item["loc"]
            if len(loc) > 3 and loc[1] == "goal_map" and loc[3] in branch_names:
                loc = [*loc[:3], *loc[4:]]
            if len(loc) > 3 and loc[1] == "steps" and loc[3] in {"ProposedStep", "ProposedCodeRepair"}:
                loc = [*loc[:3], *loc[4:]]
            item = {**item, "loc": loc}
            if item not in errors:
                errors.append(item)
        error.detail["errors"] = errors
        step_errors = [item for item in errors if item["loc"][1:2] == ["steps"]]
        if step_errors:
            error.detail["step_fields"] = list(ProposedStep.model_fields)
            error.detail["step_instruction"] = (
                "Proposed Steps require key, not the stored Step.id field. "
                "Retain an existing Step with {key:existing_step_id}; the program reuses its frozen contents. "
                "Do not copy stored id/logical_id/immutable_frozen metadata into a proposal. "
                "A full new-Step record uses key/tool/parameters and its declared dependencies; "
                "only that full form uses logical_key to retain a prior logical_id. "
                "The repair_of shorthand inherits logical_id and must omit logical_key/tool/parameters.")
            if any(len(item["loc"]) > 2 and item["loc"][2] in repair_indexes for item in step_errors):
                error.detail["code_repair_fields"] = list(ProposedCodeRepair.model_fields)
                error.detail["code_repair_instruction"] = (
                    "Use {key:new_key,repair_of:failed_step_id,code:CodeRef,inputs?:InputManifest}; "
                    "simplified-1 native repairs use input instead of code. "
                    "Omitted inputs retain the frozen manifest; explicit inputs replace only authorized task copies. "
                    "Code/native input or input copies must change. The latest settled failed Attempt's other "
                    "contract fields stay frozen. Verified settled dependencies "
                    "are carried automatically; declare any unsettled dependencies explicitly. "
                    "Python/native repairs require their respective execution permission and every existing repair gate.")
        target_errors = [item for item in errors if item["loc"][1:2] == ["goal_map"]]
        if target_errors:
            error.detail.update(
                code="goal_target_shape", path=target_errors[0]["loc"][:3],
                requirement="Each Goal target must use exactly one binding shape. "
                    "A Step binding is a future output route, not goal completion; do not combine it with a gap.",
                binding_shapes=goal_target_shapes())
            error.detail["step_target_instruction"] = (
                "A stored Goal route uses step_id; the equivalent proposal route uses step_key "
                "with that same existing Step ID and unchanged port. Verified settled Steps may be carried; "
                "retain an unsettled referenced Step explicitly with {key:existing_step_id}. "
                "Do not copy step_id into goal_map.")
            # Union validation also reports a missing 'evidence' for a malformed
            # Step route. Explain EvidenceRef only when one was actually supplied.
            targets = parameters.get("goal_map", {})
            if isinstance(targets, dict) and any(
                    isinstance(targets.get(item["loc"][2]), dict)
                    and isinstance(targets[item["loc"][2]].get("evidence"), dict)
                    for item in target_errors if len(item["loc"]) > 2):
                error.detail["evidence_reference_shapes"] = EvidenceRef.model_json_schema()["oneOf"]
                error.detail["evidence_instruction"] = (
                    "A concrete EvidenceRef requires its existing run_id and result_id, plus the unchanged port; "
                    "artifact_id/sha256 alone is only a file reference, not a Result binding. "
                    "Copy source identities from the recorded Result, never invent them. "
                    "Alternatively retain the existing completed Step with {key:existing_step_id} and keep "
                    "that Goal's route using step_key=stored step_id and the unchanged port. "
                    "A future producer_step_id cannot be mixed "
                    "with concrete Run/Result/Artifact identities. Do not replace satisfied evidence with a gap.")
        raise error from None


def _resolve_step_keys(keys, ids, history, *, path, indexed=False):
    """Resolve graph references after verified settled history was carried."""
    paths = [path + [index] if indexed else path for index in range(len(keys))]
    for key, location in zip(keys, paths, strict=True):
        if not isinstance(key, str):
            raise ProposalError("A symbolic Step reference must be a string key from the proposed Steps.",
                code="invalid_step_reference", path=location, expected_type="string")
    missing = [(key, location) for key, location in zip(keys, paths, strict=True) if key not in ids]
    if missing:
        missing_keys = list(dict.fromkeys(key for key, _ in missing))
        historical = [key for key in missing_keys if key in history]
        raise ProposalError(
            "Every referenced Step must exist in the complete Plan. Verified settled history and its closure "
            "may be carried automatically; retain unsettled Steps explicitly with {key:existing_step_id}. "
            "Their original dependencies remain unchanged. Declare a new Step "
            "with its Tool and parameters instead of referring to an unknown key.",
            code="missing_step_reference", path=missing[0][1],
            missing_step_keys=missing_keys, known_history_step_keys=historical,
            missing_references=[{"key": key, "path": location} for key, location in missing],
            retained_step_shapes=[{"key": key} for key in historical])
    return [ids[key] for key in keys]


def _code_repair_source(store, run, item, history, *, index):
    """Resolve only a trusted latest settled failure; this grants no execution."""
    path = ["parameters", "steps", index]
    from orca_agent.file_references import simplified_workflow

    native = item.input is not None
    if native and not simplified_workflow(run):
        raise ProposalError("Native repair shorthand requires workflow_policy=simplified-1.",
                            code="native_repair_policy", path=path)
    if not (run.permission.scientific_execution if native else run.permission.code_execution):
        raise ProposalError("Task code repair requires code execution permission.",
                            code="code_repair_not_authorized", path=path)
    if item.key in history or any(attempt.step_id == item.key for attempt in run.attempts) or any(
            call.step_id == item.key for call in run.calls):
        raise ProposalError("A code repair needs a new Step key, not a historical Step ID.",
                            code="code_repair_existing_key", path=[*path, "key"])
    attempts = [attempt for attempt in run.attempts if attempt.step_id == item.repair_of]
    attempt = attempts[-1] if attempts else None
    latest = ([other for other in run.attempts if other.logical_id == attempt.logical_id][-1]
              if attempt else None)
    kind = "orca" if native else "python"
    if (attempt is None or latest is not attempt or attempt.execution_kind != kind
            or attempt.state not in {"failed", "timed_out"} or attempt.finished_at is None
            or attempt.result_id is None or attempt.result_id not in run.result_ids
            or attempt.frozen_step is None
            or (attempt.frozen_step.id, attempt.frozen_step.logical_id, attempt.frozen_step.tool)
                != (attempt.step_id, attempt.logical_id, attempt.tool)
            or f"execute_{kind}" not in get_tool(attempt.tool).effects
            or not get_tool(attempt.tool).check_contract.get("generic_observations")):
        raise ProposalError(
            "repair_of must identify this Run's latest settled failed or timed-out matching Attempt's frozen "
            "Step, with its recorded Result. Unknown, unfinished, successful or superseded references cannot repair.",
            code="invalid_code_repair_reference", path=[*path, "repair_of"])
    result = store.load_result(run.id, attempt.result_id)
    if ((result.id, result.run_id, result.step_id, result.attempt_id, result.operation_status)
            != (attempt.result_id, run.id, attempt.step_id, attempt.id, attempt.state)):
        raise ProposalError("The repair Result must belong to this exact failed Attempt and Step.",
                            code="code_repair_result_mismatch", path=[*path, "repair_of"])
    return attempt.frozen_step


def _parameter_file_refs(parameters):
    """Declared code/manifest references only; never search arbitrary parameter text."""
    for name in ("code", "input"):
        if isinstance(parameters.get(name), dict):
            yield parameters[name]
    manifest = parameters.get("inputs")
    if isinstance(manifest, dict):
        yield from (entry for entry in manifest.get("entries", []) if isinstance(entry, dict))


def _normalize_producer_reference(reference, ids, history, *, path):
    """One exact symbolic producer resolver for every proposal reference role."""
    selectors = [key for key in ("producer_key", "producer_step_id") if key in reference]
    if not selectors:
        return
    canonical_ids = {**{identifier: identifier for identifier in ids.values()}, **ids}
    resolved = [_resolve_step_keys([reference[key]], canonical_ids, history, path=[*path, key])[0]
                for key in selectors]
    if len(set(resolved)) != 1:
        raise ProposalError("producer_key and producer_step_id must identify the same declared Step.",
                            path=path, code="conflicting_producer_reference")
    reference.pop("producer_key", None)
    reference["producer_step_id"] = resolved[0]


def _file_reference_items(parameters):
    for name in ("code", "input"):
        if isinstance(parameters.get(name), dict):
            yield parameters[name], [name]
    manifest = parameters.get("inputs")
    if isinstance(manifest, dict) and isinstance(manifest.get("entries"), list):
        for index, entry in enumerate(manifest["entries"]):
            if isinstance(entry, dict):
                yield entry, ["inputs", "entries", index]


def _normalize_file_role_aliases(item, definition, ids, history, *, path):
    """Consume structural aliases using the Tool's single declared file contract.

    No filename, source, permission or scientific scope is inferred. In particular,
    an outer geometry is redundant only when its exact source is already an
    explicit manifest copy; the normal preflight still checks its qualification.
    """
    if not definition.check_contract.get("generic_observations"):
        return
    if "input_manifest" in definition.input_roles and "input_manifest" in item.inputs:
        alias = item.inputs["input_manifest"]
        for reference, location in _file_reference_items({"inputs": alias}):
            _normalize_producer_reference(reference, ids, history,
                path=[*path, "inputs", "input_manifest", *location[1:]])
        current = item.parameters.get("inputs")
        for reference, location in _file_reference_items({"inputs": current}):
            _normalize_producer_reference(reference, ids, history, path=[*path, "parameters", *location])
        if current is not None and current != alias:
            raise ProposalError("Declare one consistent input manifest through its role or parameter.",
                                path=[*path, "inputs", "input_manifest"], code="conflicting_file_reference")
        item.parameters["inputs"] = alias
        item.inputs.pop("input_manifest")
    for reference, location in _file_reference_items(item.parameters):
        _normalize_producer_reference(reference, ids, history, path=[*path, "parameters", *location])
    for role, field in (("code", "code"), ("native_input", "input")):
        if role not in definition.input_roles or role not in item.inputs:
            continue
        alias = item.inputs[role]
        _normalize_producer_reference(alias, ids, history, path=[*path, "inputs", role])
        current = item.parameters.get(field)
        if current is not None:
            if (not isinstance(current, dict)
                    or any(current[key] != alias[key] for key in current.keys() & alias.keys())
                    or {key: current.get(key) for key in ("artifact_id", "producer_step_id", "port", "file_key")}
                    != {key: alias.get(key) for key in ("artifact_id", "producer_step_id", "port", "file_key")}):
                raise ProposalError("The file input role and parameter must select the same exact source.",
                    path=[*path, "inputs", role], code="conflicting_file_reference")
        item.parameters[field] = {**alias, **(current or {})}
        item.inputs.pop(role)
    if "input_manifest" not in definition.input_roles:
        return
    geometries = [(item.geometry, [*path, "geometry"])] if item.geometry is not None else []
    if "geometry" in item.inputs:
        geometries.append((item.inputs["geometry"], [*path, "inputs", "geometry"]))
    for geometry, location in geometries:
        _normalize_producer_reference(geometry, ids, history, path=location)
        try:
            reference = InputRef.model_validate(geometry)
        except ValidationError as exc:
            raise _schema_error(exc, path=location) from None
        matched = any(
            (reference.artifact_id is not None and entry.get("artifact_id") == reference.artifact_id
             or reference.producer_step_id is not None
             and entry.get("producer_step_id") == reference.producer_step_id
             and entry.get("port") == reference.port)
            for entry, entry_path in _file_reference_items(item.parameters) if entry_path[0] == "inputs")
        if not matched:
            raise ProposalError(
                "A file-manifest Tool needs the geometry source in parameters.inputs.entries with explicit "
                "target_path and purpose. An outer geometry alias must match that exact source; "
                "it cannot authorize a file or replace the managed initial-geometry preflight.",
                path=location, code="geometry_manifest_reference_required",
                manifest_path=[*path, "parameters", "inputs", "entries"])
    if geometries:
        item.geometry = None
        item.inputs.pop("geometry", None)


def _materialize_file_parameters(store, run, parameters, definition, ids, history, path):
    """Fill known immutable metadata and translate explicit producer keys."""
    from orca_agent.file_references import simplified_workflow

    if not definition.check_contract.get("generic_observations"):
        return []
    dependencies = []
    for reference, location in _file_reference_items(parameters):
        reference_path = [*path, *location]
        producer_key = reference.get("producer_key", reference.get("producer_step_id"))
        shorthand = producer_key is not None or "sha256" not in reference
        if shorthand and not simplified_workflow(run):
            raise ProposalError("File shorthand requires workflow_policy=simplified-1.", path=path)
        if producer_key is not None:
            _normalize_producer_reference(reference, ids, history, path=reference_path)
            identifier = reference["producer_step_id"]
            dependencies.append(identifier)
        elif simplified_workflow(run) and reference.get("artifact_id"):
            identifier = reference["artifact_id"]
            authorized = set(run.permission.artifact_ids) | store.verified_own_artifacts(run)
            if identifier not in authorized:
                raise ProposalError("Task input is outside the permission snapshot.",
                                    path=[*reference_path, "artifact_id"])
            artifact = store.load_artifact(identifier)
            store.artifact_path(identifier)
            if "sha256" in reference and reference["sha256"] != artifact.sha256:
                raise ProposalError("Task input hash differs from pinned Artifact.",
                                    path=[*reference_path, "sha256"])
            reference["sha256"] = artifact.sha256
        if reference is parameters.get("code") or reference is parameters.get("input"):
            expected = "python" if "execute_python" in definition.effects else "orca"
            if reference.get("language") is None and simplified_workflow(run):
                reference["language"] = expected
            if reference.get("language", expected) != expected:
                raise ProposalError("Explicit file language conflicts with the consumer Tool.", path=path)
    return dependencies


def _complete_incremental_plan(store, run, proposal, prior, request, history):
    """Carry exact settled history; never insert an omitted pending operation."""
    from orca_agent.file_references import simplified_workflow
    from orca_agent.planning import settled_step_snapshots

    settled = settled_step_snapshots(store, run)
    supplied = {item.key for item in proposal.steps}
    replaced = {item.repair_of for item in proposal.steps if isinstance(item, ProposedCodeRepair)}
    replacements = {item.repair_of: item.key for item in proposal.steps if isinstance(item, ProposedCodeRepair)}
    latest_attempts = {item.logical_id: item for item in run.attempts}
    for item in proposal.steps:
        if not isinstance(item, ProposedStep) or item.tool is None or item.key in history:
            continue
        try:
            definition = get_tool(item.tool)
        except ValueError:
            continue  # Materialization below reports the grounded Tool-field diagnostic.
        if (not definition.check_contract.get("generic_observations")
                or not set(definition.effects) & {"execute_python", "execute_orca"}):
            continue
        source = latest_attempts.get(item.logical_key)
        frozen = settled.get(source.step_id) if source else None
        if (source and frozen
                and source.state in {"failed", "timed_out"} and source.finished_at is not None
                and (frozen.id, frozen.logical_id, frozen.tool)
                == (source.step_id, source.logical_id, item.tool)):
            # The full repair form has the same existing validation boundary:
            # validate_revision still permits changing only the code reference.
            replaced.add(source.step_id)
            replacements[source.step_id] = item.key
    carried = set()

    def carry(identifier):
        if (not isinstance(identifier, str) or identifier in supplied or identifier in carried
                or identifier in replaced or identifier not in settled):
            return
        carried.add(identifier)
        for dependency in settled[identifier].depends_on:
            carry(dependency)

    requested = {goal.id: goal for goal in request.goals}
    if prior:
        for step in prior.steps:
            carry(step.id)
        for identifier, binding in prior.goal_map.items():
            if identifier in proposal.goal_map or identifier not in requested or binding.port != requested[identifier].port:
                continue
            if binding.step_id in replaced or (binding.evidence
                    and binding.evidence.producer_step_id in replaced):
                if not simplified_workflow(run):
                    continue
                source_id = binding.step_id or binding.evidence.producer_step_id
                key = replacements[source_id]
                if binding.evidence:
                    proposal.goal_map[identifier] = EvidenceGoalTarget(
                        evidence=EvidenceRef(producer_step_id=key, port=binding.evidence.port,
                            rule_version=binding.evidence.rule_version), port=binding.port)
                else:
                    proposal.goal_map[identifier] = FutureGoalTarget(step_key=key, port=binding.port)
                continue
            if binding.gap is not None:
                proposal.goal_map[identifier] = GapGoalTarget(gap=binding.gap, port=binding.port)
            elif binding.step_id in settled:
                proposal.goal_map[identifier] = FutureGoalTarget(step_key=binding.step_id, port=binding.port)
                carry(binding.step_id)
            elif binding.evidence:
                reference = binding.evidence
                if reference.producer_step_id in settled:
                    proposal.goal_map[identifier] = EvidenceGoalTarget(evidence=reference, port=binding.port)
                    carry(reference.producer_step_id)
                elif reference.result_id and (reference.result_id in run.permission.result_ids
                        or reference.run_id == run.id and reference.result_id in run.result_ids):
                    try:
                        result = store.load_result(reference.run_id, reference.result_id)
                        if result.run_id == run.id and result.step_id in replaced:
                            continue
                        terminal = {"completed", "failed", "cancelled", "timed_out"}
                        own_settled = any(record.result_id == result.id and record.state == result.operation_status
                            and record.state in terminal and (not hasattr(record, "finished_at") or record.finished_at is not None)
                            and (result.attempt_id if hasattr(record, "finished_at") else result.call_id) == record.id
                            for record in [*run.calls, *run.attempts])
                        if ((result.run_id, result.id) == (reference.run_id, reference.result_id)
                                and result.operation_status in terminal
                                and (reference.run_id != run.id or own_settled)):
                            proposal.goal_map[identifier] = EvidenceGoalTarget(evidence=reference, port=binding.port)
                    except (KeyError, ValueError, OSError, RuntimeError):
                        pass
    for item in proposal.steps:
        if isinstance(item, ProposedCodeRepair):
            if item.repair_of in history:
                for dependency in history[item.repair_of].depends_on:
                    carry(dependency)
            references = {"code": item.code.model_dump(exclude_none=True) if item.code else None,
                          "input": item.input.model_dump(exclude_none=True) if item.input else None,
                          "inputs": item.inputs.model_dump() if item.inputs else None}
            for reference in _parameter_file_refs(references):
                carry(reference.get("producer_key") or reference.get("producer_step_id"))
            continue
        for dependency in item.depends_on:
            carry(dependency)
        if item.key in history:
            for dependency in history[item.key].depends_on:
                carry(dependency)
        for reference in [item.geometry, *item.inputs.values(), *_parameter_file_refs(item.parameters)]:
            if isinstance(reference, dict):
                carry(reference.get("producer_key") or reference.get("producer_step_id"))
    for target in proposal.goal_map.values():
        carry(target.step_key if isinstance(target, FutureGoalTarget) else
              target.evidence.producer_step_id if isinstance(target, EvidenceGoalTarget) else None)
    proposal.steps.extend(ProposedStep(key=identifier) for identifier in sorted(carried))
    return settled


def settled_immediate_goal_binding(run, request, goal, selection):
    """Project an already assessed current Goal; never discover a new Result."""
    from orca_agent.file_references import simplified_workflow
    from orca_agent.tools.registry import supports_goal_output

    result = selection.get("result")
    if (not simplified_workflow(run) or result is None or selection.get("gaps")
            or (selection.get("assessment") or {}).get("status") != "passed"
            or result.run_id != run.id or result.id not in run.result_ids
            or result.operation_status != "completed" or not result.call_id):
        return None
    calls = [call for call in run.calls if call.id == result.call_id]
    if len(calls) != 1:
        return None
    call = calls[0]
    if (call.state != "completed" or call.result_id != result.id or not call.frozen_step
            or call.step_id != result.step_id or call.frozen_step.id != result.step_id
            or call.tool != call.frozen_step.tool or not supports_goal_output(call.frozen_step, goal)):
        return None
    if call.request_version != request.version:
        # A selected input may retain its original preparation across an
        # authenticated method choice. current_goal_evidence already checked
        # its original Call, current binding and unchanged physical purpose.
        selected = [binding for system_id, ports in run.input_bindings.items()
                    if system_id in goal.system_ids for port, binding in ports.items()
                    if port == goal.port and binding.get("result_id") == result.id
                    and binding.get("request_version") == request.version
                    and binding.get("source_request_version") == call.request_version]
        if len(selected) != 1:
            return None
    existing = selection.get("binding")
    reference = existing.evidence if existing else None
    if reference and reference.run_id == run.id and reference.result_id == result.id:
        reference = reference.model_dump(mode="json", exclude_none=True)
    else:
        reference = {"run_id": run.id, "result_id": result.id, "port": goal.port,
                     "rule_version": goal.minimum_check_version}
    return {"port": goal.port, "evidence": reference}


def _current_immediate_goal_bindings(store, run, request, prior):
    from orca_agent.goals import current_goal_evidence

    bindings = {}
    for goal in request.goals:
        selection = current_goal_evidence(store, run, request, goal, prior)
        binding = settled_immediate_goal_binding(run, request, goal, selection)
        if binding:
            bindings[goal.id] = binding
    return bindings


def _canonicalize_settled_goal(proposal, goal, binding, request, audits):
    original = proposal.goal_map.get(goal.id)
    if original and original.model_dump(exclude_none=True) == binding:
        return
    value = original.model_dump(mode="json", exclude_none=True) if original else None
    if audits is not None:
        audits.append({"rule": "settled-immediate-goal-binding-1", "goal_id": goal.id,
            "request_id": request.id, "request_version": request.version,
            "original_binding_sha256": fingerprint(value),
            "original_route": ({key: value[key] for key in ("step_key", "port") if key in value}
                               | {"kind": "gap" if "gap" in value else "future" if "step_key" in value else "evidence"})
                              if value else {"kind": "omitted"},
            "binding": copy.deepcopy(binding)})
    proposal.goal_map[goal.id] = EvidenceGoalTarget.model_validate(binding)


def _goal_output_route_feedback(goal, run, steps, ids, target_step=None, settled_binding=None):
    """Describe real producer routes without equating an output label to a quantity."""
    from orca_agent.tools.registry import declared_outputs, supports_goal_output

    keys = {identifier: key for key, identifier in ids.items()}
    producers = [{"step_key": keys[step.id], "tool": step.tool, "port": goal.port}
                 for step in steps if supports_goal_output(step, goal)]
    route = {"existing_producers": producers,
        "validation": "Routing advice only; existing permission, budget, file and scientific checks still apply.",
        "instruction": "Keep the original Goal quantity, port, conditions and minimum evidence. "
                       "Use the current verified evidence binding or a Step that actually declares this port; "
                       "changing a target label does not create an output."}
    if settled_binding:
        route["settled_evidence_binding"] = settled_binding
    if (goal.evidence_contract_version == "generic-observation-1"
            and goal.verification == "reported_with_limitations"
            and goal.minimum_check_version == "generic-observation-1"):
        adapters = [step for step in steps
                    if get_tool(step.tool).check_contract.get("named_observation_collector") == "task_manifest"]
        route["observation_adapters"] = [{"step_key": keys[step.id], "tool": step.tool,
            "required_observation_name": goal.port} for step in adapters]
        route["available_observation_tools"] = [name for name in run.permission.allowed_tools
            if get_tool(name).check_contract.get("named_observation_collector") == "task_manifest"
            ] if run.permission.code_execution else []
        route["observation_instruction"] = (
            "An explicitly executed observation adapter may read the real producer's pinned files/result, "
            "include the unchanged Goal port in observation_names, and emit a sourced observation record "
            "with that exact name and the requested value. Bind the Goal to that adapter Step. "
            "This is reported data, not new scientific qualification; retain all conditions, sources and limitations. "
            "Do not relabel the original producer or the Goal to make them match.")
    if target_step:
        route["selected_producer"] = {"step_key": keys[target_step.id], "tool": target_step.tool,
            "declared_outputs": declared_outputs(target_step),
            "named_observation_collector": get_tool(target_step.tool).check_contract.get("named_observation_collector")}
    return {"expected": goal.port, "valid_route": route}


def materialize_plan(store, run, parameters, *, binding_canonicalizations=None):
    from orca_agent.file_references import (
        FileReferenceError,
        declared_attempt_step,
        simplified_workflow,
        validate_file_dependencies,
    )

    proposal = _validate_plan_parameters(copy.deepcopy(parameters))
    prior = store.load_plan(run)
    request = store.load_request(run)
    settled_bindings = _current_immediate_goal_bindings(store, run, request, prior)
    if run.input_bindings:
        from orca_agent.input_bindings import resolved_request
        request = resolved_request(store, run, request)
    existing = {s.id: s for s in prior.steps} if prior else {}
    history = {a.frozen_step.id: declared_attempt_step(a) for a in run.attempts if a.frozen_step}
    history.update({c.frozen_step.id: c.frozen_step for c in run.calls if c.frozen_step})
    history.update(existing)
    settled = _complete_incremental_plan(store, run, proposal, prior, request, history)
    for goal in request.goals:
        target = proposal.goal_map.get(goal.id)
        if goal.id in settled_bindings and (target is None or isinstance(target, GapGoalTarget)):
            _canonicalize_settled_goal(proposal, goal, settled_bindings[goal.id], request,
                                      binding_canonicalizations)
    missing = [goal for goal in request.goals if goal.required and goal.id not in proposal.goal_map]
    if simplified_workflow(run):
        for goal in missing:
            proposal.goal_map[goal.id] = GapGoalTarget(port=goal.port,
                gap="The current Plan does not yet provide this required goal's evidence.")
        missing = []
    unknown = set(proposal.goal_map) - {goal.id for goal in request.goals}
    if missing:
        raise ProposalError(
            "Map every required Request Goal ID to exactly one binding shape with its unchanged port. "
            "A planned Step output is a route, not completion; use a gap only when no valid route or evidence exists.",
            path=["parameters", "goal_map"],
            missing_goals=[{"goal_id": goal.id, "port": goal.port} for goal in missing],
            binding_shapes=goal_target_shapes(),
            gap_bindings={goal.id: {"port": goal.port, "gap": "<describe missing evidence or capability>"}
                          for goal in missing})
    if unknown:
        raise ProposalError("Use only current Request Goal IDs; a Plan cannot introduce a Goal.",
                            code="unknown_goal_id", path=["parameters", "goal_map"],
                            allowed_goal_ids=[goal.id for goal in request.goals])
    keys = [s.key for s in proposal.steps]
    if len(set(keys)) != len(keys):
        raise ValueError("proposal Step keys must be unique")
    ids = {key: key if key in history else new_id("step") for key in keys}
    known_logical = {s.logical_id for s in history.values()}
    logical = {}
    repairs = {item.key: _code_repair_source(store, run, item, history, index=index)
               for index, item in enumerate(proposal.steps) if isinstance(item, ProposedCodeRepair)}
    for item in proposal.steps:
        if isinstance(item, ProposedCodeRepair):
            logical[item.key] = repairs[item.key].logical_id
            continue
        label = item.logical_key or item.key
        logical[item.key] = (history[item.key].logical_id if item.key in history else
                             label if label in known_logical else new_id("logical"))
    steps = []
    for index, item in enumerate(proposal.steps):
        if isinstance(item, ProposedCodeRepair):
            source = repairs[item.key]
            _resolve_step_keys(source.depends_on, ids, history,
                path=["parameters", "steps", index, "repair_of"])
            values = source.model_dump(mode="json")
            values["id"] = ids[item.key]
            field = "input" if item.input is not None else "code"
            replacement = item.input if item.input is not None else item.code
            values["parameters"][field] = replacement.model_dump(mode="json", exclude_none=True)
            if "language" not in replacement.model_fields_set and simplified_workflow(run):
                values["parameters"][field].pop("language", None)
            if item.inputs is not None:
                values["parameters"]["inputs"] = item.inputs.model_dump(mode="json")
            declared = item.sdk_orca if item.sdk_orca is not None else getattr(source.parameters, "sdk_orca", None)
            if field == "code" and declared is None:
                raise ProposalError(
                    "Declare sdk_orca=true for managed ORCA calls or false for analysis only "
                    "when repairing a legacy Python Step.",
                    code="missing_sdk_orca_declaration",
                    path=["parameters", "steps", index, "sdk_orca"])
            if field == "code":
                values["parameters"]["sdk_orca"] = declared
            elif item.sdk_orca is not None:
                raise ProposalError("sdk_orca applies only to Python repairs.",
                                    path=["parameters", "steps", index, "sdk_orca"])
            file_dependencies = _materialize_file_parameters(store, run, values["parameters"],
                get_tool(source.tool), ids, history, ["parameters", "steps", index])
            values["depends_on"] = list(dict.fromkeys([*values["depends_on"], *file_dependencies]))
            try:
                steps.append(Step.model_validate(values))
            except ValidationError as exc:
                raise _schema_error(exc, path=["parameters", "steps", index], tool=source.tool) from None
            continue
        if item.tool is None:
            if item.key not in history:
                raise ProposalError("A retained Step must reference a known existing key. "
                    "A new Step requires its Tool and parameters.", code="unknown_retained_step",
                    path=["parameters", "steps", index, "key"], step_key=item.key)
            unexpected = sorted(set(item.model_fields_set) - {"key"})
            if unexpected:
                raise ProposalError(
                    "A retained Step must use only {key:existing_step_id}, without other fields. "
                    "For every frozen Step being reused, omit depends_on, tool and parameters; its original "
                    "dependencies remain unchanged. Verified settled dependencies may be carried automatically; "
                    "retain unsettled dependencies explicitly. Submitted fields are not silently removed.",
                    code="retained_step_has_fields", path=["parameters", "steps", index],
                    step_key=item.key, unexpected_fields=unexpected, retained_step_shape={"key": item.key})
            missing_dependencies = [dependency for dependency in history[item.key].depends_on
                                    if dependency not in ids]
            if missing_dependencies:
                raise ProposalError(
                    "Reusing a Step preserves its dependencies. Only verified settled dependencies may be carried "
                    "automatically. Include each remaining dependency explicitly with {key:existing_step_id}; "
                    "unknown or unsettled dependencies cannot be inserted implicitly.",
                    code="missing_retained_dependency", path=["parameters", "steps", index, "key"],
                    retained_step_id=item.key, missing_dependency_step_ids=missing_dependencies)
            steps.append(history[item.key].model_copy(deep=True))
            continue
        try:
            definition = get_tool(item.tool)
        except ValueError:
            raise ProposalError("Choose a Tool from the current permitted catalog.",
                                path=["parameters", "steps", index, "tool"]) from None
        if item.tool == "python.execute" and "inputs" not in item.parameters:
            source = next((attempt.frozen_step for attempt in reversed(run.attempts)
                           if attempt.logical_id == logical[item.key]), None)
            if source:
                if source.tool != "python.execute":
                    raise ProposalError("Python input inheritance requires an existing Python Step; "
                        "a repair cannot replace its Tool.", code="invalid_code_repair_source_tool",
                        path=["parameters", "steps", index, "logical_key"])
                item.parameters["inputs"] = source.parameters.inputs.model_dump(mode="json")
        if (definition.check_contract.get("sdk_orca_declaration")
                and type(item.parameters.get("sdk_orca")) is not bool):
            raise ProposalError(
                "Declare parameters.sdk_orca=true for managed ORCA calls or false for analysis only. "
                "ORCA requires a current prepared/registered initial geometry before this code Step; "
                "stage identity/geometry preparation first and keep dependent goals as gaps.",
                code="missing_sdk_orca_declaration",
                path=["parameters", "steps", index, "parameters", "sdk_orca"])
        # The model chooses the operation and references. Repeating already
        # confirmed Request conditions is not a second scientific decision.
        # Only declared fields are inherited; explicit proposal values remain
        # visible to the existing conflict/applicability checks.
        from orca_agent.applicability import effective_conditions, preparation_conditions
        fields = definition.parameter_schema.get("properties", {})
        if "write_input_artifact" in definition.effects and "system_id" in fields:
            parameter_system = item.parameters.get("system_id")
            if parameter_system is not None and not isinstance(parameter_system, str):
                raise ProposalError("Use the declared system identifier string.",
                                    path=["parameters", "steps", index, "parameters", "system_id"])
            if item.system_id is None:
                item.system_id = parameter_system
            elif parameter_system is not None and parameter_system != item.system_id:
                raise ProposalError(
                    "Step.system_id and parameters.system_id must refer to the same requested system; "
                    "declaring either one is sufficient.",
                    path=["parameters", "steps", index, "parameters", "system_id"])
            if item.system_id is not None and item.system_id not in {system.id for system in request.systems}:
                raise ProposalError(
                    "Use a system identifier declared in the current Request.",
                    path=["parameters", "steps", index, "parameters", "system_id"])
        if (item.system_id is None and len(request.systems) == 1
                and ("system_id" in fields or set(definition.effects) & {"execute_orca", "execute_python"})):
            item.system_id = request.systems[0].id
        if "prepare_geometry" in definition.effects:
            values = preparation_conditions(request, item.system_id)["conditions"]
        else:
            mapped = [goal for goal in request.goals if isinstance(
                target := proposal.goal_map.get(goal.id), FutureGoalTarget) and target.step_key == item.key]
            values = effective_conditions(
                request, mapped[0] if len(mapped) == 1 else None, item.system_id)["conditions"]
        inherited = {key: value for key, value in values.items()
                     if key in fields and value is not None}
        if "system_id" in fields and item.system_id is not None:
            inherited["system_id"] = item.system_id
        item.parameters = {**inherited, **item.parameters}
        if simplified_workflow(run):
            _normalize_file_role_aliases(item, definition, ids, history, path=["parameters", "steps", index])
        file_dependencies = _materialize_file_parameters(store, run, item.parameters, definition,
            ids, history, ["parameters", "steps", index, "parameters"])
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
        geometry_path = ["parameters", "steps", index, "geometry"]
        bindings = dict(item.inputs)
        if "execute_orca" in definition.effects and "geometry" in bindings:
            if geometry is not None:
                raise ProposalError("Bind geometry once, either geometry or inputs.geometry.",
                                    path=["parameters", "steps", index, "inputs", "geometry"])
            geometry = bindings.pop("geometry")
            geometry_path = ["parameters", "steps", index, "inputs", "geometry"]
        if geometry:
            _normalize_producer_reference(geometry, ids, history, path=geometry_path)
        if geometry is None:
            if "geometry" in definition.input_roles and "execute_orca" in definition.effects:
                system = next((s for s in request.systems if s.id == item.system_id), None)
                artifact = system.geometry_artifact_id if system else request.geometry_artifact_id
                if artifact:
                    geometry = {"artifact_id": artifact}
        inputs = {}
        for name, value in bindings.items():
            path = ["parameters", "steps", index, "inputs", name]
            _normalize_producer_reference(value, ids, history, path=path)
            try:
                inputs[name] = EvidenceRef.model_validate(value)
            except ValidationError as exc:
                raise _schema_error(exc, path=path) from None
        dependencies = _resolve_step_keys(item.depends_on, ids, history,
            path=["parameters", "steps", index, "depends_on"], indexed=True)
        # A reference itself explicitly declares the dependency relationship.
        dependencies += [r.producer_step_id for r in inputs.values() if r.producer_step_id]
        dependencies += file_dependencies
        if geometry and geometry.get("producer_step_id"):
            dependencies.append(geometry["producer_step_id"])
        try:
            resolved_geometry = InputRef.model_validate(geometry) if geometry else None
        except ValidationError as exc:
            raise _schema_error(exc, path=geometry_path) from None
        try:
            steps.append(Step(id=ids[item.key], logical_id=logical[item.key], tool=item.tool,
                              parameters=item.parameters, system_id=item.system_id,
                              geometry=resolved_geometry,
                              depends_on=list(dict.fromkeys(dependencies)), inputs=inputs))
        except ValidationError as exc:
            raise _schema_error(exc, path=["parameters", "steps", index], tool=definition.name) from None
    goals = {}
    requested_goals = {goal.id: goal for goal in request.goals}
    for goal_id, target in proposal.goal_map.items():
        value = target.model_dump(exclude_none=True)
        requested_goal = requested_goals[goal_id]
        target_step = next((step for step in steps if step.id == ids.get(value.get("step_key"))), None)
        if target_step and goal_id in settled_bindings:
            from orca_agent.tools.registry import supports_goal_output

            if value.get("port") != requested_goal.port or not supports_goal_output(target_step, requested_goal):
                _canonicalize_settled_goal(proposal, requested_goal, settled_bindings[goal_id], request,
                                          binding_canonicalizations)
                value = proposal.goal_map[goal_id].model_dump(exclude_none=True)
        if goal_id in requested_goals and value.get("port") != requested_goals[goal_id].port:
            raise ProposalError("Preserve the requested Goal port and select a producer that really declares it. "
                "The Goal label and a Tool's existing output cannot be made equivalent by renaming either one; use the explicit routes below.",
                code="goal_output_route_mismatch", path=["parameters", "goal_map", goal_id, "port"],
                **_goal_output_route_feedback(requested_goal, run, steps, ids, target_step,
                                             settled_bindings.get(goal_id)))
        evidence = value.get("evidence", {})
        if evidence.get("producer_step_id") in ids:
            value["evidence"] = {**evidence, "producer_step_id": ids[evidence["producer_step_id"]]}
        if (evidence.get("producer_step_id") in settled
                and evidence.get("port") == value["port"]):
            from orca_agent.planning import _compatible_rule

            rule = evidence.get("rule_version")
            if rule is None or _compatible_rule(requested_goals[goal_id].minimum_check_version, rule):
                value = {"port": value["port"], "step_key": evidence["producer_step_id"]}
        if "step_key" in value:
            if value["step_key"] not in ids:
                raise ProposalError("Use an exact key from this proposed Plan's Steps.",
                    code="unknown_goal_step", path=["parameters", "goal_map", goal_id, "step_key"])
            value = {**value, "step_id": ids[value["step_key"]]}
            value.pop("step_key")
            from orca_agent.tools.registry import supports_goal_output

            target_step = next(step for step in steps if step.id == value["step_id"])
            if not supports_goal_output(target_step, requested_goal):
                raise ProposalError("The selected producer cannot collect the unchanged Goal's value. "
                    "Keep its real outputs and the original Goal; use a declared producer or an explicit observation adapter.",
                    code="goal_output_route_mismatch", path=["parameters", "goal_map", goal_id, "port"],
                    **_goal_output_route_feedback(requested_goal, run, steps, ids, target_step,
                                                 settled_bindings.get(goal_id)))
        try:
            goals[goal_id] = OutputBinding.model_validate(value)
        except ValidationError as exc:
            raise _schema_error(exc, path=["parameters", "goal_map", goal_id]) from None
    previous = [d for d in run.decisions if d.get("plan_version") is not None]
    previous += [{"plan_id": d["prior_plan_id"], "plan_version": d["prior_plan_version"]}
                 for d in run.decisions if d.get("prior_plan_version") is not None]
    last = max(previous, key=lambda d: d["plan_version"]) if previous else {}
    try:
        plan = Plan(id=prior.id if prior else last.get("plan_id") or new_id("plan"),
                    version=prior.version + 1 if prior else last.get("plan_version", 0) + 1,
                    request_id=request.id, request_version=request.version, steps=steps, goal_map=goals)
    except ValidationError as exc:
        raise _schema_error(exc, path=["parameters"]) from None
    try:
        validate_file_dependencies(plan, run)
    except FileReferenceError as exc:
        raise ProposalError(**exc.detail) from None
    return plan
