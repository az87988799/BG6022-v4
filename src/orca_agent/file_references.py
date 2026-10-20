"""Resolve explicit Plan file dependencies without executing or choosing files."""

from __future__ import annotations

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


def _producer(plan, step, reference):
    producer = next((item for item in plan.steps if item.id == reference.producer_step_id), None)
    if producer is None or producer.id == step.id or producer.id not in step.depends_on:
        raise ValueError("file reference requires an explicit producer Step dependency")
    definition = get_tool(producer.tool)
    if reference.port == "written_artifact":
        if (not definition.check_contract.get("code_authoring")
                or "written_artifact" not in definition.observation_outputs):
            raise ValueError("written_artifact requires a file-writing producer")
    elif reference.port == "prepared_geometry":
        if ("prepare_geometry" not in definition.effects
                or "prepared_geometry" not in definition.output_ports):
            raise ValueError("prepared_geometry requires a qualified geometry producer")
        if step.system_id is None or producer.system_id != step.system_id:
            raise ValueError("prepared_geometry producer and consumer must use the same explicit system")
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
                    if (reference.port == "written_artifact"
                            and producer.parameters.role != expected):
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
    if reference.port == "prepared_geometry":
        from orca_agent.input_bindings import validate_input_result

        binding = validate_input_result(store, run, request, result, "prepared_geometry")
        selected = run.input_bindings.get(producer.system_id, {}).get("prepared_geometry")
        if binding != selected or binding["system_id"] != producer.system_id:
            raise ValueError("prepared_geometry differs from the current selected system input")
        return binding["artifact_id"], binding["sha256"]
    if reference.port == "written_artifact":
        if record.state != "completed":
            raise ValueError("written_artifact requires a completed file-writing Call")
        value = result.observations.get("written_artifact", {})
    else:
        if result.observations.get("evidence_collection_complete") is not True:
            raise ValueError("file_key requires complete archived execution evidence")
        value = result.source.get("files", {}).get(reference.file_key, {})
    if (not isinstance(value, dict) or not value.get("artifact_id") or not value.get("sha256")
            or value["artifact_id"] not in result.artifact_ids):
        raise ValueError("selected producer file does not exist in its archived Result")
    artifact = store.load_artifact(value["artifact_id"])
    if artifact.run_id != run.id:
        raise ValueError("producer Artifact belongs to another Run")
    if reference.port == "written_artifact":
        if artifact.source.get("call_id") != record.id:
            raise ValueError("written Artifact is not bound to its producing Call")
    elif artifact.attempt_id != record.id:
        raise ValueError("archived file is not bound to its producing Attempt")
    return value["artifact_id"], value["sha256"]


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
        if isinstance(reference, ProducerFileRef):
            producer = _producer(plan, step, reference)
            result, record = _result(store, run, producer, allow_pending=allow_pending)
            if result is None:
                return declaration
            identifier, digest = _artifact_for_producer(
                store, run, request, producer, reference, result, record)
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
        return concrete

    values["parameters"][parameter_key] = resolve(references[0], parameter_key,
        "python" if parameter_key == "code" else "orca")
    values["parameters"]["inputs"]["entries"] = [
        resolve(reference, f"inputs/{index}") for index, reference in enumerate(references[1:])]
    return Step.model_validate(values)
