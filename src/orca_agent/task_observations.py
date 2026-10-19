"""Read a bounded task declaration and bind it to archived evidence, never qualify it."""

import json
from pathlib import PurePosixPath

from pydantic import Field

from orca_agent.models import Check, Observation, ObservationSource, Record


class _Source(Record):
    filename: str = Field(strict=True, min_length=1, max_length=240)
    internal_request_key: str | None = None
    location: str | None = Field(default=None, max_length=1000)


class _Declared(Record):
    name: str
    value: object
    unit: str | None = Field(default=None, max_length=128)
    sources: list[_Source] = Field(min_length=1, max_length=16)
    formula: str | None = Field(default=None, max_length=4000)
    artifacts: list[str] = Field(default_factory=list, max_length=64)
    limitations: list[str] = Field(default_factory=list, max_length=16)


class _Manifest(Record):
    version: int
    observations: list[object] = Field(max_length=32)


def _receipt_parent(run, receipt, attempt):
    """A reused source retains its original, settled Python provenance."""
    parent = next((item for item in run.attempts if item.id == receipt.parent_attempt_id), None)
    if parent is None:
        raise ValueError("observation internal source has no original Attempt")
    if parent.id != attempt.id:
        if (receipt.state != "completed" or receipt.finished_at is None
                or parent.finished_at is None or parent.state in {"intent", "running", "unknown"}
                or parent.execution_kind != "python" or parent.frozen_step is None
                or parent.request_version != attempt.request_version
                or not getattr(parent.frozen_step.parameters, "code", None)
                or receipt.code is None or receipt.code.language != "orca"):
            raise ValueError("reused observation source lacks settled original provenance")
    return parent


def _task_relative(name):
    if (not isinstance(name, str) or not name or PurePosixPath(name).is_absolute()
            or "\\" in name or ":" in name
            or any(p in {"", ".", ".."} for p in name.split("/"))):
        raise ValueError("observation source is not a task-relative file")
    return name


def _record(store, run, step, attempt, files, item):
    sources, native_conditions = [], []
    for declaration in item.sources:
        name = _task_relative(declaration.filename)
        admitted = {}
        if declaration.internal_request_key:
            receipt = next((r for r in run.internal_calls
                if r.request_key == declaration.internal_request_key), None)
            if receipt is None or receipt.state not in {"completed", "failed", "timed_out", "cancelled"}:
                raise ValueError("observation internal source has no settled receipt")
            parent = _receipt_parent(run, receipt, attempt)
            if parent.id != attempt.id:
                native_input = store.load_artifact(receipt.code.artifact_id)
                store.artifact_path(native_input.id)
                if ((native_input.run_id, native_input.attempt_id) != (run.id, parent.id)
                        or native_input.sha256 != receipt.code.sha256):
                    raise ValueError("reused observation native input provenance changed")
            candidates = [store.load_artifact(a) for a in receipt.artifact_ids]
            candidates = [a for a in candidates if PurePosixPath(a.path).name == name]
            if len(candidates) != 1:
                raise ValueError("internal source filename is missing or ambiguous")
            artifact = candidates[0]
            admitted = dict(receipt.actual_conditions)
            native_conditions.append(admitted)
        else:
            parent = attempt
            selected = files.get("task/" + name)
            if selected is None:
                raise ValueError("observation task source was not archived")
            artifact = store.load_artifact(selected["artifact_id"])
        store.artifact_path(artifact.id)
        if artifact.run_id != run.id or artifact.attempt_id != parent.id:
            raise ValueError("observation source does not belong to its original Attempt")
        sources.append(ObservationSource(artifact_id=artifact.id, sha256=artifact.sha256,
            location=declaration.location, attempt_id=parent.id,
            internal_request_key=declaration.internal_request_key, actual_conditions=admitted))
    artifacts = []
    for name in item.artifacts:
        selected = files.get("task/" + _task_relative(name))
        if selected is None:
            raise ValueError("observation analysis artifact was not archived")
        artifact = store.load_artifact(selected["artifact_id"])
        store.artifact_path(artifact.id)
        if (artifact.run_id, artifact.attempt_id) != (run.id, attempt.id):
            raise ValueError("observation analysis artifact belongs to a different Attempt")
        artifacts.append(artifact.id)
    # Different candidate geometries are expected in a comparison. Preserve
    # every actual input binding; the summary contains only shared conditions.
    conditions = dict(native_conditions[0]) if native_conditions else {}
    for admitted in native_conditions[1:]:
        conditions = {name: value for name, value in conditions.items()
                      if name in admitted and type(value) is type(admitted[name]) and value == admitted[name]}
    comparable = {"method", "basis", "charge", "multiplicity", "electronic_state", "environment",
                  "origin", "request_version", "system_id", "run_type", "atom_count",
                  "input_keywords", "input_blocks"}
    conflicts = sorted(name for name in comparable if native_conditions and any(
        type(admitted.get(name)) is not type(native_conditions[0].get(name))
        or admitted.get(name) != native_conditions[0].get(name) for admitted in native_conditions[1:]))
    checks = [Check(name="archived_source_binding", status="passed", rule_version="generic-observation-1",
                    detail="file identity/hash only; value, units and scientific interpretation are unverified")]
    limitations = [
        "Generated code parsed these values; no registered scientific checker qualified them.",
        "Source locations and interpretation are task declarations; only archived file identity is checked.",
        "Conditions describe admitted input, not independent output verification.", *item.limitations]
    if conflicts:
        detail = "Source conditions or calculation stages conflict: " + ", ".join(conflicts)
        checks.append(Check(name="source_conditions_consistent", status="failed",
                            rule_version="generic-observation-1", detail=detail))
        limitations.append(detail)
    return Observation(name=item.name, value=item.value, unit=item.unit, actual_conditions=conditions,
        sources=sources, code_sha256=step.parameters.code.sha256, formula=item.formula,
        artifact_ids=list(dict.fromkeys(artifacts)), checks=checks,
        validation_status="failed" if conflicts else "unverified", limitations=limitations)


def collect(store, run, step, attempt, files, *, diagnostics=None):
    entry = files.get("task/observations.json")
    if entry is None:
        return []
    path = store.artifact_path(entry["artifact_id"])
    if path.stat().st_size > 65536:
        raise ValueError("observations.json exceeds 64 KiB")
    from orca_agent.llm import _strict_json

    raw = _strict_json(path.read_bytes())
    if not isinstance(raw, dict) or type(raw.get("version")) is not int or raw["version"] != 1:
        raise ValueError("unsupported task observation manifest")
    manifest = _Manifest.model_validate(raw)
    records = []
    names = set()
    duplicates = {item.get("name") for item in manifest.observations if isinstance(item, dict)
                  and isinstance(item.get("name"), str)
                  and sum(isinstance(other, dict) and other.get("name") == item.get("name")
                          for other in manifest.observations) > 1}
    for index, declaration in enumerate(manifest.observations):
        try:
            item = _Declared.model_validate(declaration)
            if (item.name not in step.parameters.observation_names or item.name in names
                    or item.name in duplicates):
                raise ValueError("observation name is duplicate or outside the frozen Step")
            names.add(item.name)
            json.dumps(item.value, allow_nan=False)
            records.append(_record(store, run, step, attempt, files, item))
        except (ValueError, TypeError, KeyError, OSError, RuntimeError) as exc:
            if diagnostics is None:
                raise
            diagnostics.append({"category": "observation_manifest_rejected", "index": index,
                "name": declaration.get("name") if isinstance(declaration, dict) else None, "detail": str(exc)})
    return records


def _native_current_conditions(request, goal, system_id):
    """Normalize this profile's spellings before comparing all condition scopes.

    This is a read-only projection. Provenance and genuine conflicts still go
    through the existing resolver; historical scientific checks are unchanged.
    """
    from orca_agent.applicability import CONDITION_FIELDS, effective_conditions
    from orca_agent.orca.native_profile import canonical_native_condition

    def canonical(name, value):
        # Retain RHF's explicit restricted-state assertion for the resolver's
        # method/electronic_state consistency check.
        if name == "method" and isinstance(value, str) and value.strip().casefold() == "rhf":
            return "RHF"
        return canonical_native_condition(name, value)

    def mapping(conditions):
        return {name: canonical(name, value) if name in CONDITION_FIELDS else value
                for name, value in conditions.items()}

    projected = request.model_copy(deep=True, update={
        **{name: canonical(name, getattr(request, name))
           for name in CONDITION_FIELDS if hasattr(request, name)},
        "conditions": mapping(request.conditions),
        "systems": [system.model_copy(deep=True, update={"conditions": mapping(system.conditions)})
                    for system in request.systems],
    })
    constraint = goal.model_copy(deep=True, update={"conditions": mapping(goal.conditions)})
    return effective_conditions(projected, constraint, system_id)


def _additional_conditions(request, goal, system_id):
    """No unimplemented physical condition becomes true by being unqualified."""
    from orca_agent.applicability import CONDITION_FIELDS

    # These are presentation requirements, not claims about a native calculation.
    metadata = {"explain_results", "requires_sources"}
    system = next((item for item in request.systems if item.id == system_id), None)
    values, sources, reasons = {}, {}, []
    for scope in (request, system):
        if scope is None:
            continue
        for name, value in scope.conditions.items():
            if name not in CONDITION_FIELDS and name not in metadata:
                values[name] = value
                sources[name] = scope.conditions_source.get(name)
    for name, value in goal.conditions.items():
        if name not in CONDITION_FIELDS and name not in metadata:
            if name in values and (type(values[name]) is not type(value) or values[name] != value):
                reasons.append("conflicting_goal_condition:" + name)
            values.setdefault(name, value)
    for name, value in values.items():
        if value is None or sources.get(name) in {"unknown", "inferred", "not_applicable"}:
            reasons.append("unknown_condition:" + name)
        if name != "geometry_relation":
            reasons.append("native_condition_not_verified:" + name)
        elif not isinstance(value, str) or value not in {"fixed_initial", "optimized"}:
            reasons.append("unsupported_geometry_relation")
    return values.get("geometry_relation"), reasons


def _geometry_relation_evidence(store, run, request, step, attempt, observation, receipts, system_id, relation):
    """Verify input/output roles; do not qualify numerical geometry or convergence."""
    from orca_agent.input_bindings import prepared_geometry
    from orca_agent.orca.native import _xyz_file, read_pinned_input

    keys = {source.internal_request_key for source in observation.sources if source.internal_request_key}
    selected = [receipt for receipt in receipts if receipt.request_key in keys]
    if len(selected) != 1 or selected[0].state != "completed":
        raise ValueError("geometry_relation_requires_one_completed_native_source")
    receipt = selected[0]
    parent = _receipt_parent(run, receipt, attempt)
    source_step = parent.frozen_step or step
    binding = receipt.conditions.get("input_binding", {})
    if (not isinstance(binding, dict) or receipt.conditions.get("admission_version") != "native-orca-input-2"
            or any(binding.get(name) != expected for name, expected in {
                "request_id": request.id, "request_version": request.version,
                "step_id": source_step.id, "system_id": system_id}.items())):
        raise ValueError("geometry_relation_input_purpose_binding_missing")
    system = next((item for item in request.systems if item.id == system_id), None)
    geometry_id = (prepared_geometry(store, run, request, system_id)
                   if system and system.geometry_source == "prepare" else
                   system.geometry_artifact_id if system else request.geometry_artifact_id)
    if geometry_id is None or binding.get("geometry_artifact_id") != geometry_id:
        raise ValueError("geometry_relation_initial_geometry_binding_changed")
    initial = store.load_artifact(geometry_id)
    initial_bytes = read_pinned_input(store.artifact_path(geometry_id), initial.sha256, limit=65536)
    if (binding.get("geometry_sha256") != initial.sha256
            or receipt.actual_conditions.get("input_geometry_sha256") != initial.sha256):
        raise ValueError("geometry_relation_initial_geometry_hash_changed")
    dependencies = [entry for entry in receipt.inputs.entries
                    if entry.target_path.lower().endswith(".xyz") and entry.sha256 == initial.sha256]
    if len(dependencies) != 1:
        raise ValueError("geometry_relation_initial_dependency_missing")
    dependency = dependencies[0]
    read_pinned_input(store.artifact_path(dependency.artifact_id), dependency.sha256, limit=65536)

    def archived(name):
        matches = [store.load_artifact(identifier) for identifier in receipt.artifact_ids]
        matches = [artifact for artifact in matches if PurePosixPath(artifact.path).name == name]
        if len(matches) != 1 or matches[0].size == 0:
            raise ValueError("geometry_relation_native_file_missing_or_ambiguous:" + name)
        artifact = matches[0]
        if (artifact.run_id, artifact.attempt_id) != (run.id, parent.id):
            raise ValueError("geometry_relation_native_file_provenance_mismatch")
        store.artifact_path(artifact.id)
        return artifact

    stdout = archived("stdout.out")
    cited = {source.artifact_id for source in observation.sources
             if source.internal_request_key == receipt.request_key}
    run_type = receipt.actual_conditions.get("run_type")
    if relation == "fixed_initial":
        if run_type != "single_point" or stdout.id not in cited:
            raise ValueError("fixed_initial_requires_bound_single_point_output")
    elif relation == "optimized":
        if run_type != "optimization_and_analytic_frequencies":
            raise ValueError("optimized_requires_optimization_native_output")
        if dependency.target_path.casefold() == "job.xyz":
            raise ValueError("optimized_geometry_output_overlaps_initial_input")
        final = archived("job.xyz")
        final_bytes = read_pinned_input(store.artifact_path(final.id), final.sha256, limit=65536)
        if _xyz_file(initial_bytes) != _xyz_file(final_bytes):
            raise ValueError("optimized_geometry_atom_count_differs_from_initial")
        native_results = {stdout.id, final.id}
        for identifier in receipt.artifact_ids:
            if PurePosixPath(store.load_artifact(identifier).path).name == "job.hess":
                native_results.add(archived("job.hess").id)
        if not cited & native_results:
            raise ValueError("optimized_requires_original_native_result_source")
    else:
        raise ValueError("unsupported_geometry_relation")


def _declared_scientific_conditions(request, goal):
    """Separate requested physics from incidental historical Request defaults."""
    from orca_agent.applicability import CONDITION_FIELDS

    declared = set(goal.conditions) | set(request.conditions) | set(request.automatic_selection)
    declared |= set(request.selection_reasons)
    # semantic_defaults is only the authorized normalization template. Applied
    # defaults are recorded in conditions and in request.<field> evidence.
    for key in request.condition_evidence:
        scope, separator, name = key.partition(".")
        if not separator:
            declared.add(key)  # Historical unscoped evidence.
        elif scope in {"request", "automatic_selection"}:
            declared.add(name)
    for name in CONDITION_FIELDS:
        origin = request.conditions_source.get(name)
        if origin in {"explicit", "auto_selected", "inherited", "inferred", "unknown"}:
            declared.add(name)
        elif name in type(request).model_fields:
            value = getattr(request, name)
            if value is not None and value != type(request).model_fields[name].default:
                declared.add(name)
    return sorted(declared.intersection(CONDITION_FIELDS))


def _same_archived_observations(rebuilt, recorded):
    """Compare evidence without adding metadata to a historical observation."""
    if len(rebuilt) != len(recorded):
        return False
    for current, historical in zip(rebuilt, recorded, strict=True):
        current_value, old_value = current.model_dump(mode="json"), historical.model_dump(mode="json")
        if len(current.sources) != len(historical.sources):
            return False
        for source, old_source in zip(current_value["sources"], historical.sources, strict=True):
            if "actual_conditions" not in old_source.model_fields_set:
                source.pop("actual_conditions", None)
        if current_value != old_value:
            return False
    return True


def assess(store, run, request, goal, result):
    """Assess only a sourced delivery contract; scientific requirements stay hard."""
    from orca_agent.minimum_evidence import assess_minimum_evidence

    reasons, conditions = [], {}
    try:
        if goal.verification != "reported_with_limitations":
            raise ValueError("generic_observation_cannot_satisfy_qualified_evidence")
        if request.unresolved or goal.unresolved or result.operation_status != "completed":
            raise ValueError("unresolved_purpose_or_incomplete_source_operation")
        if result.run_id != run.id or result.id not in run.result_ids:
            raise ValueError("generic_observation_requires_current_run_provenance")
        attempt = next((a for a in run.attempts if a.id == result.attempt_id), None)
        if (attempt is None or attempt.state != "completed" or attempt.result_id != result.id
                or attempt.request_version != request.version or attempt.frozen_step is None
                or not getattr(attempt.frozen_step.parameters, "code", None)):
            raise ValueError("generic_observation_attempt_purpose_mismatch")
        step = attempt.frozen_step
        system_ids = goal.system_ids or [s.id for s in request.systems]
        system_id = step.system_id or (request.systems[0].id if len(request.systems) == 1 else None)
        if system_ids and (len(system_ids) != 1 or system_id != system_ids[0]):
            raise ValueError("generic_observation_system_binding_mismatch")
        if result.observations.get("evidence_collection_complete") is not True:
            raise ValueError("generic_observation_collection_incomplete")
        if result.source.get("code_sha256") != step.parameters.code.sha256:
            raise ValueError("generic_observation_code_binding_mismatch")
        store.artifact_path(step.parameters.code.artifact_id)
        if store.load_artifact(step.parameters.code.artifact_id).sha256 != step.parameters.code.sha256:
            raise ValueError("generic_observation_code_hash_mismatch")
        rebuilt = collect(store, run, step, attempt, result.source["files"], diagnostics=[])
        selected = [item for item in rebuilt if item.name == goal.port]
        recorded = [item for item in result.observation_records if item.name == goal.port]
        if not _same_archived_observations(selected, recorded):
            raise ValueError("generic_observation_differs_from_archived_manifest")
        if len(selected) != 1 or selected[0].value is None:
            raise ValueError("requested_observation_missing")
        observation = selected[0]
        if observation.validation_status == "failed" or any(check.status == "failed" for check in observation.checks):
            raise ValueError("requested_observation_has_failed_consistency_checks")
        conditions = observation.actual_conditions
        source_keys = {source.internal_request_key for source in observation.sources
                       if source.internal_request_key}
        receipts = [r for r in run.internal_calls
                    if r.parent_attempt_id == attempt.id or r.request_key in source_keys]
        relation, extra_reasons = _additional_conditions(request, goal, system_id)
        reasons.extend(extra_reasons)
        if receipts:
            keys = {source.internal_request_key for source in observation.sources
                    if source.internal_request_key}
            if not keys or any(r.state != "completed" for r in receipts if r.request_key in keys):
                raise ValueError("requested_observation_lacks_completed_native_source")
            if (conditions.get("origin") != "admitted_native_input"
                    or conditions.get("request_version") != request.version
                    or conditions.get("system_id") != system_id
                    or store.run_configuration(run).execution.native_input_policy is None):
                raise ValueError("native_input_purpose_binding_missing_or_changed")
            from orca_agent.orca.native_profile import canonical_native_condition
            assessment = _native_current_conditions(request, goal, system_id)
            reasons.extend(assessment["reasons"])
            for name, expected in assessment["conditions"].items():
                canonical = canonical_native_condition(name, expected)
                if canonical is None or canonical != canonical_native_condition(name, conditions.get(name)):
                    reasons.append("native_source_condition_missing_or_mismatch:" + name)
            if relation is not None:
                _geometry_relation_evidence(store, run, request, step, attempt, observation,
                                            receipts, system_id, relation)
        elif request.systems:
            raise ValueError("scientific_system_observation_lacks_native_evidence")
        elif request.geometry_artifact_id is not None:
            raise ValueError("scientific_geometry_observation_lacks_native_evidence")
        elif relation is not None:
            raise ValueError("geometry_relation_requires_native_evidence")
        else:
            reasons.extend("scientific_condition_requires_native_evidence:" + name
                           for name in _declared_scientific_conditions(request, goal))
    except (ValueError, TypeError, KeyError, OSError, RuntimeError) as exc:
        reasons.append(str(exc))
    requirements = assess_minimum_evidence(goal, result, purpose_passed=not reasons)
    reasons.extend(item["reason"] + ":" + item["requested"] for item in requirements
                   if item["status"] != "passed")
    return {"status": "unresolved" if reasons else "passed", "reasons": list(dict.fromkeys(reasons)),
            "minimum_evidence": requirements, "scientific_qualification": False,
            "current_use": {"source_conditions": conditions,
                "source_condition_evidence": {"origin": "admitted_native_input" if conditions else "unknown",
                    "scientific_output_verification": "unverified"}}}
