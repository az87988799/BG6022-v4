"""Resolve explicit Plan file dependencies without executing or choosing files."""

from __future__ import annotations

from pathlib import PurePosixPath, PureWindowsPath

from orca_agent.models import (
    ArtifactCodeSelection,
    ArtifactInputSelection,
    Attempt,
    CodeRef,
    InputManifestEntry,
    ProducerFileRef,
    Step,
    fingerprint,
)
from orca_agent.tools.registry import get_tool


class FileReferenceError(ValueError):
    """A settled, unstarted dependency selection can be corrected by planning.

    Provenance, permission, unknown execution and hash failures deliberately do
    not use this type: they must retain the ordinary hard execution rejection.
    """

    def __init__(self, requirement, **detail):
        super().__init__(requirement)
        self.detail = {"requirement": requirement, **detail}


def _safe_file_key(value):
    return (isinstance(value, str) and 0 < len(value) <= 240
            and not PurePosixPath(value).is_absolute()
            and not any(char in value for char in '\\:<>"|?*')
            and not any(ord(char) < 32 or ord(char) == 127 for char in value)
            and all(part not in {"", ".", ".."} and not part.endswith((" ", "."))
                    and not PureWindowsPath(part).is_reserved() for part in value.split("/")))


def _selected_file(result, reference):
    """Resolve only the exact archive key or its one task-root spelling.

    There is no basename search, recursive path normalization, or selection of
    another producer/Attempt. An explicit task/ key remains exact.
    """
    key = reference.file_key
    detail = {"producer_step_id": reference.producer_step_id, "result_id": result.id}
    if not _safe_file_key(key):
        raise FileReferenceError("Use an unambiguous relative producer file_key; traversal and host paths are forbidden.",
                                 code="invalid_producer_file_key", **detail)
    from orca_agent.llm import _has_credential

    if not _has_credential(key):
        detail["requested_file_key"] = key
    files = result.source.get("files", {})
    if not isinstance(files, dict):
        raise ValueError("producer file inventory is invalid")
    candidates = [name for name in (key, "task/" + key)
                  if name in files and (name == key or not key.startswith(("task/", "sealed/", "internal/")))]
    if not candidates:
        names = [name for name in files if _safe_file_key(name) and not _has_credential(name)]
        names.sort(key=lambda name: (name.rsplit("/", 1)[-1] != key.rsplit("/", 1)[-1], name))
        raise FileReferenceError("Selected producer file does not exist in its archived Result; choose an exact available file_key or revise its producer.",
            code="producer_file_missing", available_file_keys=names[:8], available_file_count=len(names), **detail)
    if len(candidates) > 1:
        values = [files[name] for name in candidates]
        if any(not isinstance(value, dict) for value in values):
            raise ValueError("producer file inventory binding is invalid")
        identities = {value.get("artifact_id") for value in values}
        digests = {value.get("sha256") for value in values}
        if len(identities) > 1:
            raise FileReferenceError("Producer file_key is ambiguous between archive and task-relative spellings; choose an exact archived key.",
                code="producer_file_ambiguous", available_file_keys=[name for name in candidates
                    if not _has_credential(name)], **detail)
        if len(digests) > 1:
            raise ValueError("producer file aliases have conflicting pinned hashes")
    return candidates[0], files[candidates[0]]


def simplified_workflow(run):
    return getattr(getattr(run, "configuration", None), "workflow_policy", None) == "simplified-1"


def file_references(step):
    parameters = step.parameters
    code = getattr(parameters, "code", None) or getattr(parameters, "input", None)
    return [code, *parameters.inputs.entries] if code is not None else []


def declared_attempt_step(attempt):
    """The approved symbolic contract, distinct from its pinned execution copy."""
    value = attempt.consumption.get("_declared_step")
    return Step.model_validate(value) if value is not None else attempt.frozen_step


def _file_writer(producer):
    definition = get_tool(producer.tool)
    return (definition.check_contract.get("code_authoring")
            and "written_artifact" in definition.observation_outputs)


def _producer(plan, step, reference):
    producer = next((item for item in plan.steps if item.id == reference.producer_step_id), None)
    if producer is None or producer.id == step.id or producer.id not in step.depends_on:
        raise ValueError("file reference requires an explicit producer Step dependency")
    definition = get_tool(producer.tool)
    if reference.port == "written_artifact":
        if not _file_writer(producer):
            raise ValueError("written_artifact requires a file-writing producer")
    elif reference.port == "prepared_geometry":
        if ("prepare_geometry" not in definition.effects
                or "prepared_geometry" not in definition.output_ports):
            raise ValueError("prepared_geometry requires a qualified geometry producer")
        if step.system_id is None or producer.system_id != step.system_id:
            raise ValueError("prepared_geometry producer and consumer must use the same explicit system")
    elif _file_writer(producer):
        # A writer declares one immutable file. Its exact name and its output
        # port identify the same file; keep the caller's original declaration.
        if not _safe_file_key(reference.file_key) or reference.file_key != producer.parameters.name:
            raise FileReferenceError(
                "Writer file_key must exactly match its declared name; use that name or the written_artifact port.",
                code="writer_file_key_mismatch", producer_step_id=producer.id)
    elif (not definition.check_contract.get("generic_observations")
          or not set(definition.effects) & {"execute_python", "execute_orca"}):
        raise ValueError("file_key requires an explicit managed execution producer")
    return producer


def validate_file_dependencies(plan, run):
    """Pure declaration checks also apply before any producer has executed."""
    for step in plan.steps:
        for reference in file_references(step):
            if isinstance(reference, (ArtifactCodeSelection, ArtifactInputSelection, ProducerFileRef)):
                if not simplified_workflow(run):
                    raise ValueError("file selection shorthand requires workflow_policy=simplified-1")
            if isinstance(reference, (ArtifactCodeSelection, ArtifactInputSelection)):
                raise ValueError("concrete Artifact shorthand must be materialized to a hash before saving a Plan")
            if isinstance(reference, ProducerFileRef):
                producer = _producer(plan, step, reference)
                expected = ("python" if hasattr(step.parameters, "code") else "orca")
                is_code = reference is file_references(step)[0]
                if is_code:
                    if reference.port == "prepared_geometry":
                        raise ValueError("qualified geometry cannot be used as executable code")
                    if reference.language not in {None, expected}:
                        raise ValueError("explicit file language conflicts with consumer Tool")
                    if _file_writer(producer) and producer.parameters.role != expected:
                        raise ValueError("writer role conflicts with consumer Tool language")


def _result(store, run, producer, *, allow_pending):
    records = [record for record in [*run.calls, *run.attempts] if record.step_id == producer.id]
    if not records or not records[-1].result_id:
        if allow_pending:
            return None, None
        raise ValueError("file producer has no settled Result")
    record = records[-1]
    if (record.state not in {"completed", "failed", "timed_out"}
            or record.result_id not in run.result_ids
            or run.selected_results.get(producer.id) != record.result_id
            or record.frozen_step is None or record.tool != producer.tool
            or isinstance(record, Attempt) and record.finished_at is None):
        raise ValueError("file producer Result is not selected and settled in this Run")
    frozen = declared_attempt_step(record) if isinstance(record, Attempt) else record.frozen_step
    if frozen != producer:
        raise ValueError("file producer differs from its frozen Plan Step")
    result = store.load_result(run.id, record.result_id)
    if not isinstance(record, Attempt) and record.parameters != frozen.parameters.model_dump(mode="json"):
        raise ValueError("file producer Call parameters differ from its frozen Step")
    source = result.attempt_id if isinstance(record, Attempt) else result.call_id
    if ((result.run_id, result.step_id, result.id, source, result.operation_status)
            != (run.id, producer.id, record.result_id, record.id, record.state)):
        raise ValueError("file producer Result/Call/Attempt binding mismatch")
    return result, record


def _artifact_for_producer(store, run, request, producer, reference, result, record):
    resolved_key = None
    writer = _file_writer(producer)
    if reference.port == "prepared_geometry":
        from orca_agent.input_bindings import validate_input_result

        binding = validate_input_result(store, run, request, result, "prepared_geometry")
        selected = run.input_bindings.get(producer.system_id, {}).get("prepared_geometry")
        if binding != selected or binding["system_id"] != producer.system_id:
            raise ValueError("prepared_geometry differs from the current selected system input")
        return binding["artifact_id"], binding["sha256"], resolved_key
    if writer:
        if isinstance(record, Attempt) or record.state != "completed":
            raise ValueError("written_artifact requires a completed file-writing Call")
        value = result.observations.get("written_artifact", {})
    else:
        if result.observations.get("evidence_collection_complete") is not True:
            raise ValueError("file_key requires complete archived execution evidence")
        resolved_key, value = _selected_file(result, reference)
    if (not isinstance(value, dict) or not value.get("artifact_id") or not value.get("sha256")
            or value["artifact_id"] not in result.artifact_ids):
        raise ValueError("selected producer file does not exist in its archived Result")
    artifact = store.load_artifact(value["artifact_id"])
    if artifact.run_id != run.id:
        raise ValueError("producer Artifact belongs to another Run")
    if writer:
        if artifact.source.get("call_id") != record.id:
            raise ValueError("written Artifact is not bound to its producing Call")
        if any(value.get(field) != getattr(producer.parameters, field)
               or artifact.source.get(field) != getattr(producer.parameters, field)
               for field in ("name", "role")):
            raise ValueError("written Artifact name or role differs from its producing Step")
    elif artifact.attempt_id != record.id:
        raise ValueError("archived file is not bound to its producing Attempt")
    if (resolved_key is not None and resolved_key != reference.file_key
            and artifact.source.get("task_path", resolved_key) != resolved_key):
        raise ValueError("task-relative producer alias differs from its archived original file key")
    return value["artifact_id"], value["sha256"], resolved_key


def resolve_step_files(store, run, step, *, request=None, plan=None, allow_pending=False, sources=None):
    """Return a concrete execution copy; unresolved approved producers stay symbolic only in Plans."""
    references = file_references(step)
    if not references:
        return step
    if not simplified_workflow(run) and all(isinstance(reference, (CodeRef, InputManifestEntry))
                                           for reference in references):
        return step
    plan = plan or store.load_plan(run)
    request = request or store.load_request(run)
    if plan is None:
        raise ValueError("file references require an approved or proposed Plan")
    validate_file_dependencies(plan, run)
    authorized = set(run.permission.artifact_ids) | store.verified_own_artifacts(run)
    values = step.model_dump(mode="json")
    parameter_key = "code" if hasattr(step.parameters, "code") else "input"

    def resolve(reference, location, expected_language=None):
        declaration = reference.model_dump(mode="json")
        result = None
        resolved_key = None
        if isinstance(reference, ProducerFileRef):
            producer = _producer(plan, step, reference)
            result, record = _result(store, run, producer, allow_pending=allow_pending)
            if result is None:
                return declaration
            try:
                identifier, digest, resolved_key = _artifact_for_producer(
                    store, run, request, producer, reference, result, record)
            except FileReferenceError as exc:
                path = (["parameters", "inputs", "entries", int(location.split("/")[1])]
                        if location.startswith("inputs/") else ["parameters", location])
                exc.detail.update(step_id=step.id, path=path)
                raise
        else:
            identifier = reference.artifact_id
            if identifier not in authorized:
                raise ValueError("task input is outside the permission snapshot")
            digest = getattr(reference, "sha256", None)
        artifact = store.load_artifact(identifier)
        store.artifact_path(identifier)  # validates stored bytes against the Artifact hash
        if digest is not None and digest != artifact.sha256:
            raise ValueError("task input hash differs from pinned Artifact")
        if expected_language:
            language = getattr(reference, "language", None)
            if language not in {None, expected_language}:
                raise ValueError("explicit file language conflicts with consumer Tool")
            inferred = {"code": "python", "native_input": "orca"}.get(artifact.role)
            if inferred is not None and inferred != expected_language:
                raise ValueError("Artifact role conflicts with consumer Tool language")
            if isinstance(reference, ArtifactCodeSelection) and inferred is None:
                raise ValueError("omitted code language requires a typed code or native input Artifact")
            concrete = CodeRef(artifact_id=identifier, sha256=artifact.sha256, language=expected_language,
                               version=getattr(reference, "version", 1)).model_dump(mode="json")
        else:
            concrete = InputManifestEntry(artifact_id=identifier, sha256=artifact.sha256,
                target_path=reference.target_path, purpose=reference.purpose).model_dump(mode="json")
        if sources is not None and isinstance(reference, (ProducerFileRef, ArtifactCodeSelection, ArtifactInputSelection)):
            sources[location] = {"declaration": declaration, "artifact_id": identifier, "sha256": artifact.sha256}
            if result is not None:
                sources[location].update(run_id=run.id, result_id=result.id, result_fingerprint=fingerprint(result))
                if resolved_key is not None and resolved_key != reference.file_key:
                    sources[location]["resolved_file_key"] = resolved_key
        return concrete

    values["parameters"][parameter_key] = resolve(references[0], parameter_key,
        "python" if parameter_key == "code" else "orca")
    values["parameters"]["inputs"]["entries"] = [
        resolve(reference, f"inputs/{index}") for index, reference in enumerate(references[1:])]
    return Step.model_validate(values)
