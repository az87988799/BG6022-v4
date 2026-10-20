"""Authorized condition choices backed by retrieved evidence, within one Run."""

import hashlib
import json
import re
from copy import deepcopy
from typing import Any, Literal

from pydantic import Field, ValidationError

from orca_agent.models import Identifier, Record, Request, fingerprint, require_electronic_integer
from orca_agent.proposals import ProposalError, _schema_error
from orca_agent.store import StoreError

ConditionName = Literal["method", "basis", "charge", "multiplicity", "electronic_state", "environment",
                        "temperature_K", "standard_state"]
SELECTION_VERSION = "condition-selection-1"
GENERAL_SELECTION_VERSION = "condition-selection-2"
SIMPLIFIED_SELECTION_VERSION = "condition-selection-3"


def simplified_workflow(run):
    """Only the persisted Run policy enables new rules; model text cannot opt in."""
    return getattr(run.configuration, "workflow_policy", None) == "simplified-1"


def general_admission_enabled(store, run):
    configuration = store.run_configuration(run) if store is not None else run.configuration
    policy = configuration.execution.native_input_policy if configuration is not None else None
    return policy is not None and policy.profile == "managed-native-1"


def selection_version(store, run):
    if simplified_workflow(run):
        return SIMPLIFIED_SELECTION_VERSION
    return GENERAL_SELECTION_VERSION if general_admission_enabled(store, run) else SELECTION_VERSION


def selection_available(store, run):
    """Identity queries establish a graph, independently of method selection."""
    from orca_agent.tools.registry import get_tool

    if simplified_workflow(run):
        return _selection_before_science(store, run)
    if run.attempts:
        return False
    general = general_admission_enabled(store, run)
    for call in run.calls:
        effects = get_tool(call.tool).effects
        if "write_input_artifact" not in effects:
            continue
        reusable_input = ("query_external_identity" in effects
                          or simplified_workflow(run) and "prepare_geometry" in effects)
        if (not general or not reusable_input
                or call.state not in {"completed", "failed", "cancelled"}):
            return False
    return True


def _selection_before_science(store, run):
    """Preparation does not close selection; uncertain/scientific execution does.

    The context's store-less call only exposes a proposal. Activation rechecks
    the immutable Results and execution receipts under the existing Run lock.
    """
    from orca_agent.tools.registry import get_tool

    if run.usage.orca_starts_actual or run.internal_calls:
        return False
    for call in run.calls:
        if call.state not in {"completed", "failed"} or call.result_id not in run.result_ids:
            return False
        if "execute_orca" in get_tool(call.tool).effects:
            return False
    for attempt in run.attempts:
        step = attempt.frozen_step
        if (attempt.state not in {"completed", "failed", "cancelled", "timed_out", "not_started"}
                or attempt.finished_at is None or attempt.result_id not in run.result_ids
                or step is None or (step.id, step.logical_id, step.tool)
                != (attempt.step_id, attempt.logical_id, attempt.tool)):
            return False
        effects = get_tool(step.tool).effects
        if attempt.execution_kind == "python":
            # SDK capability is not execution: every native launch first
            # persists an InternalCall, already excluded above.
            if "execute_python" not in effects:
                return False
        elif ("execute_orca" not in effects or attempt.started
              or attempt.state not in {"failed", "cancelled", "not_started"}):
            return False
    if store is None:
        return True
    try:
        for attempt in run.attempts:
            result = store.load_result(run.id, attempt.result_id)
            if not _selection_attempt_evidence(store, run, attempt, result):
                return False
        for call in run.calls:
            result = store.load_result(run.id, call.result_id)
            if ((result.id, result.run_id, result.call_id, result.step_id, result.operation_status)
                    != (call.result_id, run.id, call.id, call.step_id, call.state)):
                return False
            # Registered identity/prepared geometry are inputs, not completed
            # scientific outputs. Other qualified sources keep conditions fixed.
            if result.qualified_outputs and not set(get_tool(call.tool).effects) & {
                    "query_external_identity", "prepare_geometry"}:
                return False
    except (OSError, ValueError, StoreError):
        return False
    return True


def _selection_attempt_evidence(store, run, attempt, result):
    """Bind a settled non-scientific Attempt to its original archived receipt."""
    if ((result.id, result.run_id, result.attempt_id, result.step_id, result.operation_status)
            != (attempt.result_id, run.id, attempt.id, attempt.step_id, attempt.state)
            or result.qualified_outputs
            or result.observations.get("evidence_collection_complete") is not True):
        return False
    execution = result.source.get("execution", {})
    if (not isinstance(execution, dict) or execution.get("state") != attempt.state
            or execution.get("reconciliation") or execution.get("collection_reconciliation")
            or execution.get("control_action")):
        return False
    handle = execution.get("handle")
    if handle is not None and not isinstance(handle, dict):
        return False
    if attempt.execution_kind == "orca" and (
            execution.get("not_started") is not True
            or execution.get("actual_start_status") not in {None, "not_started"}
            or (handle or {}).get("actual_start_evidence")):
        return False
    step = attempt.frozen_step
    code = getattr(step.parameters, "code" if attempt.execution_kind == "python" else "input", None)
    if (code is None or result.source.get("code_sha256") != code.sha256
            or attempt.consumption.get("code") != code.model_dump(mode="json")):
        return False
    store.artifact_path(code.artifact_id)
    if store.load_artifact(code.artifact_id).sha256 != code.sha256:
        return False
    receipt = result.source.get("files", {}).get("execution.json", {})
    artifact_id = receipt.get("artifact_id")
    if artifact_id not in result.artifact_ids:
        return False
    artifact = store.load_artifact(artifact_id)
    if (artifact.run_id, artifact.attempt_id, artifact.sha256) != (
            run.id, attempt.id, receipt.get("sha256")):
        return False
    path = store.artifact_path(artifact_id)
    return path.stat().st_size <= 1024 * 1024 and json.loads(path.read_bytes()) == execution

# Keep punctuation belonging to a chemical identifier, including basis suffixes
# such as (d,p). Closing prose parentheses and sentence punctuation delimit it.
# Unicode word/dash characters prevent accepting an ASCII prefix of another name.
_IDENTIFIER = re.compile(r"\w[\w+*/\-\u2010-\u2015\u2212]*(?:\([\w,+*/\-\u2010-\u2015\u2212]+\)[\w+*/\-\u2010-\u2015\u2212]*)*")


def _mentions_identifier(value, quote):
    return any(match.group().casefold() == value.casefold() for match in _IDENTIFIER.finditer(quote))


class SelectionSource(Record):
    result_id: Identifier
    document_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    quote: str = Field(min_length=1, max_length=1800)


class ConditionChoice(Record):
    value: Any
    reason: str = Field(min_length=1, max_length=1200)
    sources: list[SelectionSource] = Field(min_length=1, max_length=4)


class SelectionParameters(Record):
    request_version: int = Field(strict=True, ge=1)
    choices: dict[ConditionName, ConditionChoice] = Field(min_length=1, max_length=8)


class OptionalSelectionSource(Record):
    result_id: Identifier
    document_sha256: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    quote: str | None = Field(default=None, min_length=1, max_length=1800)


class OptionalConditionChoice(Record):
    value: Any
    reason: str = Field(min_length=1, max_length=1200, pattern=r"\S")
    sources: list[OptionalSelectionSource] = Field(default_factory=list, max_length=4)


class OptionalSelectionParameters(Record):
    request_version: int = Field(strict=True, ge=1)
    choices: dict[ConditionName, OptionalConditionChoice] = Field(min_length=1, max_length=8)


def _parameters(run, parameters):
    schema = OptionalSelectionParameters if simplified_workflow(run) else SelectionParameters
    if isinstance(parameters, (SelectionParameters, OptionalSelectionParameters)):
        parameters = parameters.model_dump(mode="json")
    return schema.model_validate(parameters)


def selectable_conditions(request):
    """An authorization is not a value, and cannot overwrite explicit unknowns."""
    names = []
    for name in request.automatic_selection:
        source = request.conditions_source.get(name)
        if source in {"explicit", "inherited", "auto_selected", "unknown", "inferred"}:
            continue
        if any(name in system.conditions and system.conditions_source.get(name) in {
                "explicit", "inherited", "auto_selected", "unknown", "inferred"} for system in request.systems):
            continue
        if any(name in goal.conditions for goal in request.goals):
            continue
        if request.condition_evidence.get("automatic_selection." + name):
            names.append(name)
    return list(dict.fromkeys(names))


def selection_action_parameters(request):
    from orca_agent.schema_projection import project_schema
    simplified = request.condition_evidence.get("workflow_policy", {}).get("version") == "simplified-1"
    schema = project_schema((OptionalSelectionParameters if simplified else SelectionParameters).model_json_schema())
    schema["properties"]["request_version"] = {"type": "integer", "const": request.version}
    schema["properties"]["choices"]["propertyNames"] = {"enum": selectable_conditions(request)}
    return schema


def _value(name, value):
    if name in {"charge", "multiplicity"}:
        require_electronic_integer(value)
        if name == "multiplicity" and value < 1:
            raise StoreError("selected multiplicity must be positive")
    elif name == "temperature_K":
        if type(value) not in {int, float} or not 0 < value < float("inf"):
            raise StoreError("selected temperature must be finite and positive")
    elif not isinstance(value, str) or not value.strip() or len(value) > 100:
        raise StoreError("selected condition requires a bounded nonempty string")
    return value


def _selection_error(path, reason, instruction):
    path = ["parameters", *path]
    return ProposalError(instruction, path=path,
                         corrections=[{"path": path, "reason": reason, "instruction": instruction}])


def _source(store, run, reference, *, path):
    if reference.result_id not in run.result_ids:
        raise _selection_error([*path, "result_id"], "source_not_in_current_run",
            "Use a completed knowledge.search Result retrieved in this Run; external IDs are not selection evidence.")
    result = store.load_result(run.id, reference.result_id)
    calls = [call for call in run.calls if call.id == result.call_id]
    if (len(calls) != 1 or calls[0].tool != "knowledge.search" or calls[0].state != "completed"
            or calls[0].result_id != result.id or result.operation_status != "completed"):
        raise _selection_error([*path, "result_id"], "source_not_completed_bound_search",
            "Use a completed knowledge.search Result whose completed Call binds this Result; do not cite an unfinished search.")
    document = result.observations.get("document_excerpt", {})
    excerpt = document.get("excerpt")
    if document.get("status") != "found" or not isinstance(excerpt, str):
        raise _selection_error([*path, "result_id"], "source_excerpt_not_available",
            "The selected Result must contain a found document_excerpt; retrieve suitable evidence before selecting.")
    if not re.fullmatch(r"[a-f0-9]{64}", str(document.get("document_sha256", ""))):
        raise _selection_error([*path, "result_id"], "source_document_hash_missing",
            "Retrieved evidence must retain its original document hash; an omitted model field cannot invent provenance.")
    if reference.document_sha256 is not None and document.get("document_sha256") != reference.document_sha256:
        raise _selection_error([*path, "document_sha256"], "source_document_hash_mismatch",
            "Copy document_sha256 exactly from this Result's document_excerpt; a different document or excerpt hash cannot substitute.")
    quote = excerpt if reference.quote is None else reference.quote
    if not quote.strip() or quote not in excerpt:
        raise _selection_error([*path, "quote"], "source_quote_not_exact_excerpt",
            "Copy a nonblank contiguous exact substring of this Result's document_excerpt.excerpt, preserving punctuation and whitespace; do not paraphrase or add text.")
    excerpt_hash = hashlib.sha256(excerpt.encode("utf-8")).hexdigest()
    if document.get("excerpt_sha256", excerpt_hash) != excerpt_hash:
        raise _selection_error([*path, "quote"], "retrieved_excerpt_hash_mismatch",
            "The stored excerpt does not match its recorded hash; use intact retrieved evidence or retrieve again without changing historical evidence.")
    # Old official search receipts did not carry kind; their exact registered
    # URL still identifies the source. Unregistered URLs never become official.
    kind = document.get("kind")
    if kind is None:
        from orca_agent.tools.knowledge import DOCUMENT_METADATA, DOCUMENTS
        kind = "official_document" if any(document.get("url") == value[1]
            and DOCUMENT_METADATA.get(key, {}).get("kind") == "official_document"
            for key, value in DOCUMENTS.items() if isinstance(value, tuple)) else "unknown"
    return {"result_id": result.id, "result_fingerprint": fingerprint(result),
            "document_sha256": document.get("document_sha256"), "excerpt_sha256": excerpt_hash,
            "quote": quote, "kind": kind,
            **{key: document.get(key) for key in ("document_id", "index_version", "document_version", "url", "title")}}


def selected_request(store, run, prior, parameters):
    """Recompute the complete permitted revision; callers cannot broaden it."""
    parameters = _parameters(run, parameters)
    if parameters.request_version != prior.version:
        raise StoreError("condition selection refers to a stale Request")
    if set(parameters.choices) - set(selectable_conditions(prior)):
        raise StoreError("condition selection is outside the unchanged user authorization")
    if not selection_available(store, run):
        raise StoreError("condition selection requires settled preparation and no actual or uncertain scientific execution")
    values = prior.model_dump(mode="json")
    corrections = []
    for name, choice in parameters.choices.items():
        authorization = prior.condition_evidence["automatic_selection." + name]
        message = next((message for message in prior.messages
                        if message.get("id") == authorization.get("message_id")), None)
        if (message is None or not authorization.get("text_basis")
                or message["text"].count(authorization["text_basis"]) != 1):
            if (not simplified_workflow(run) or message is None or not authorization.get("text_basis")
                    or authorization["text_basis"] not in message["text"]):
                raise StoreError("condition selection authorization lacks original user evidence")
        sources = []
        invalid_source = False
        for index, source in enumerate(choice.sources):
            try:
                sources.append(_source(store, run, source, path=["choices", name, "sources", index]))
            except ProposalError as error:
                corrections.extend(error.detail["corrections"])
                invalid_source = True
        if invalid_source:
            continue
        if (not simplified_workflow(run) and name in {"method", "basis"}
                and not any(source["kind"] == "official_document" for source in sources)):
            corrections.extend(_selection_error(["choices", name, "sources"], "official_documentation_required",
                "Method/basis selection requires retrieved official documentation; project guides alone are insufficient.").detail["corrections"])
            continue
        if name in {"charge", "multiplicity", "electronic_state"}:
            # A manual supplies a procedure, not this molecule's actual state.
            # Current Step 3 selection therefore cannot infer state from parity
            # or a generic method recommendation; use explicit/authorized defaults.
            raise StoreError("electronic state selection requires molecule-specific evidence; preserve the missing condition")
        if simplified_workflow(run) and name not in {"method", "basis"} and not sources:
            raise StoreError("only delegated method/basis choices allow model reasoning without retrieved sources")
        value = _value(name, choice.value)
        if not simplified_workflow(run) and not general_admission_enabled(store, run) and name in {"method", "basis"} and not any(_mentions_identifier(value, source["quote"])
                for source in sources if source["kind"] == "official_document"):
            corrections.extend(_selection_error(["choices", name, "value"], "selected_value_absent_from_official_quote",
                "This selected value must occur as a complete identifier in a quoted official source, not a prefix of another method/basis. Retrieve and quote an exact official passage containing the value; do not invent supporting text or substitute a project guide.").detail["corrections"])
            continue
        values["conditions"][name] = value
        if name in {"method", "basis", "charge", "multiplicity"}:
            values[name] = value
        values["conditions_source"][name] = "auto_selected"
        values["selection_reasons"][name] = choice.reason
        values["condition_evidence"]["request." + name] = {
            "value": value, "source": "auto_selected", "request_version": prior.version,
            "authorization": deepcopy(authorization), "reason": choice.reason,
            "sources": sources, "schema": selection_version(store, run),
            **({"basis_kind": "retrieved_evidence" if sources else "model_reason",
                "scientific_validation": "not_established_by_selection"} if simplified_workflow(run) else {})}
    if corrections:
        raise ProposalError("Correct the identified selection fields using retrieved evidence; no condition was selected.",
                            path=["parameters", "choices"], corrections=corrections)
    values["version"] = prior.version + 1
    return Request.model_validate(values)


def validate_selection_revision(store, run, prior, proposed, record):
    if not isinstance(record, dict) or set(record) != {"schema", "parameters"} or record["schema"] != selection_version(store, run):
        raise StoreError("invalid condition selection record")
    expected = selected_request(store, run, prior, record["parameters"])
    if proposed != expected:
        raise StoreError("condition selection may only change the authorized choices and their evidence")


def commit_selections(store, run, parameters, *, decision_id, basis, related_results=None, fault=None):
    try:
        parameters = _parameters(run, parameters)
    except ValidationError as exc:
        raise _schema_error(exc, path=["parameters"]) from exc
    prior = store.load_request(run)
    proposed = selected_request(store, run, prior, parameters)
    return store.commit_revision(run, None, request=proposed, decision_id=decision_id, basis=basis,
        related_results=related_results, fault=fault,
        selection_record={"schema": selection_version(store, run), "parameters": parameters.model_dump(mode="json")})
