"""Read a bounded task declaration and bind it to archived evidence, never qualify it."""

import json
from pathlib import PurePosixPath
from types import SimpleNamespace

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


def _general_admission(store, run):
    policy = store.run_configuration(run).execution.native_input_policy
    return policy is not None and policy.profile == "managed-native-1"


def _registered_input(store, run, step, name, files):
    """Bind a task input to its original immutable source, never a rewritten copy."""
    entries = [entry for entry in step.parameters.inputs.entries if entry.target_path == name]
    if not entries:
        return None
    entry, = entries
    artifact = store.load_artifact(entry.artifact_id)
    if artifact.sha256 != entry.sha256:
        raise ValueError("observation input hash differs from the frozen manifest")
    store.artifact_path(artifact.id)
    if artifact.id not in run.permission.artifact_ids and artifact.id not in store.verified_own_artifacts(run):
        raise ValueError("observation input is outside the frozen input authorization")
    copied = files.get("task/" + name)
    if copied is not None:
        copy = store.load_artifact(copied["artifact_id"])
        store.artifact_path(copy.id)
        if copy.sha256 != entry.sha256:
            raise ValueError("observation input copy changed; cite the derived file separately")
    return artifact


def _direct_native_source(store, run, artifact):
    """Recover admitted conditions from a completed direct call's archived receipt."""
    parent = next((item for item in run.attempts if item.id == artifact.attempt_id), None)
    if artifact.run_id != run.id or parent is None or parent.tool != "orca.run":
        return None
    if (parent.state != "completed" or parent.finished_at is None or parent.result_id not in run.result_ids
            or parent.frozen_step is None or parent.request_version is None or parent.native_workload is None):
        raise ValueError("direct native source lacks completed frozen provenance")
    result = store.load_result(run.id, parent.result_id)
    if ((result.run_id, result.attempt_id, result.step_id) != (run.id, parent.id, parent.step_id)
            or result.operation_status != "completed" or artifact.id not in result.artifact_ids
            or result.observations.get("evidence_collection_complete") is not True):
        raise ValueError("direct native source Result is incomplete or mismatched")
    reference = result.source.get("files", {}).get("prepared.json", {})
    record = store.load_artifact(reference["artifact_id"])
    if ((record.run_id, record.attempt_id, record.role) != (run.id, parent.id, "execution_record")
            or record.sha256 != reference.get("sha256") or record.id not in result.artifact_ids):
        raise ValueError("direct native admission archive binding differs")
    path = store.artifact_path(record.id)
    if path.stat().st_size > 65536:
        raise ValueError("direct native admission archive exceeds its read bound")
    from orca_agent.llm import _strict_json

    prepared = _strict_json(path.read_bytes())
    workload = parent.native_workload
    if (not isinstance(prepared, dict) or any(prepared.get(key) != value for key, value in workload.items())
            or prepared.get("admission_version") not in {"native-orca-input-2", "native-orca-input-3"}
            or prepared.get("input_sha256") != parent.frozen_step.parameters.input.sha256):
        raise ValueError("direct native admission differs from the frozen workload")
    code = store.load_artifact(parent.frozen_step.parameters.input.artifact_id)
    store.artifact_path(code.id)
    if code.sha256 != prepared["input_sha256"]:
        raise ValueError("direct native source input changed")
    actual = prepared.get("actual_conditions")
    if not isinstance(actual, dict) or actual.get("request_version") != parent.request_version:
        raise ValueError("direct native admission conditions are missing or changed")
    return SimpleNamespace(request_key=None, parent_attempt_id=parent.id, state="completed",
        actual_conditions={**actual, "native_attempt_id": parent.id}, conditions=workload,
        inputs=parent.frozen_step.parameters.inputs, artifact_ids=result.artifact_ids,
        direct_attempt=parent)


def _receipt_parent(run, receipt, attempt):
    """A reused source retains its original, settled Python provenance."""
    parent = next((item for item in run.attempts if item.id == receipt.parent_attempt_id), None)
    if parent is None:
        raise ValueError("observation internal source has no original Attempt")
    if getattr(receipt, "direct_attempt", None) is parent:
        return parent
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
    general = _general_admission(store, run)
    for declaration in item.sources:
        name = _task_relative(declaration.filename)
        admitted = {}
        registered_input = None
        request_key = declaration.internal_request_key
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
            registered_input = _registered_input(store, run, step, name, files) if general else None
            if registered_input is not None:
                artifact = registered_input
                admitted = {"origin": "registered_task_input", "input_artifact_id": artifact.id,
                            "input_sha256": artifact.sha256}
                producers = [receipt for receipt in run.internal_calls if artifact.id in receipt.artifact_ids]
                if producers:
                    if len(producers) != 1 or producers[0].state != "completed":
                        raise ValueError("registered input native source is incomplete or ambiguous")
                    receipt, = producers
                    parent = _receipt_parent(run, receipt, attempt)
                    if (artifact.run_id, artifact.attempt_id) != (run.id, parent.id):
                        raise ValueError("registered input native source provenance differs")
                    request_key = receipt.request_key
                    admitted = dict(receipt.actual_conditions)
                    native_conditions.append(admitted)
                else:
                    direct = _direct_native_source(store, run, artifact)
                    if direct is not None:
                        admitted = direct.actual_conditions
                        native_conditions.append(admitted)
            else:
                selected = files.get("task/" + name)
                if selected is None:
                    raise ValueError("observation task source was not archived")
                artifact = store.load_artifact(selected["artifact_id"])
        store.artifact_path(artifact.id)
        if registered_input is None and (artifact.run_id != run.id or artifact.attempt_id != parent.id):
            raise ValueError("observation source does not belong to its original Attempt")
        sources.append(ObservationSource(artifact_id=artifact.id, sha256=artifact.sha256,
            location=declaration.location, attempt_id=artifact.attempt_id if registered_input is not None else parent.id,
            internal_request_key=request_key, actual_conditions=admitted))
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
        detail = ("Source conditions or calculation stages differ: " if general else
                  "Source conditions or calculation stages conflict: ") + ", ".join(conflicts)
        if not general:
            checks.append(Check(name="source_conditions_consistent", status="failed",
                                rule_version="generic-observation-1", detail=detail))
        limitations.append(detail)
    if any(source.actual_conditions.get("origin") == "registered_task_input" for source in sources):
        limitations.append("Registered input identity/hash is checked; its calculation conditions and scientific contents remain unknown.")
    return Observation(name=item.name, value=item.value, unit=item.unit, actual_conditions=conditions,
        sources=sources, code_sha256=step.parameters.code.sha256, formula=item.formula,
        artifact_ids=list(dict.fromkeys(artifacts)), checks=checks,
        validation_status="failed" if conflicts and not general else "unverified", limitations=limitations)


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


def _native_current_conditions(request, goal, system_id, *, policy=None):
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
        return canonical_native_condition(name, value, policy=policy) if policy else canonical_native_condition(name, value)

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


def _additional_conditions(request, goal, system_id, *, actual=None):
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
            if actual is None or name not in actual:
                reasons.append("native_condition_not_verified:" + name)
            elif type(actual[name]) is not type(value) or actual[name] != value:
                reasons.append("native_source_condition_missing_or_mismatch:" + name)
        elif not isinstance(value, str) or value not in {"fixed_initial", "optimized"}:
            reasons.append("unsupported_geometry_relation")
    return values.get("geometry_relation"), reasons


def _geometry_relation_evidence(store, run, request, step, attempt, observation, receipts, system_id, relation):
    """Verify input/output roles; do not qualify numerical geometry or convergence."""
    from orca_agent.input_bindings import prepared_geometry
    from orca_agent.orca.native import _xyz_file, read_pinned_input

    keys = {source.internal_request_key for source in observation.sources if source.internal_request_key}
    selected = [receipt for receipt in receipts if receipt.request_key in keys
                or getattr(receipt, "direct_attempt", None) is not None]
    if len(selected) != 1 or selected[0].state != "completed":
        raise ValueError("geometry_relation_requires_one_completed_native_source")
    receipt = selected[0]
    parent = _receipt_parent(run, receipt, attempt)
    source_step = parent.frozen_step or step
    binding = receipt.conditions.get("input_binding", {})
    versions = {"native-orca-input-2", "native-orca-input-3"} if _general_admission(store, run) else {"native-orca-input-2"}
    if (not isinstance(binding, dict) or receipt.conditions.get("admission_version") not in versions
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
        fixed_types = {"single_point"}
        if _general_admission(store, run):
            fixed_types.update({"analytic_frequencies", "numerical_frequencies"})
        if run_type not in fixed_types or stdout.id not in cited:
            raise ValueError("fixed_initial_requires_bound_single_point_output")
    elif relation == "optimized":
        optimizing = {"optimization_and_analytic_frequencies"}
        if _general_admission(store, run):
            optimizing.update({"optimization", "optimization_and_numerical_frequencies"})
        if run_type not in optimizing:
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


def _assess_general_sources(store, run, request, goal, step, attempt, observation):
    """Compare each admitted source with its requested scope, not with its peers."""
    from orca_agent.orca.native_profile import canonical_native_condition

    systems = {system.id: system for system in request.systems}
    requested = goal.system_ids or list(systems)
    if len(requested) != len(set(requested)) or set(requested) - systems.keys():
        raise ValueError("generic_observation_system_binding_mismatch")
    if step.system_id is not None and (step.system_id not in systems
            or requested and requested != [step.system_id]):
        raise ValueError("generic_observation_system_binding_mismatch")
    keys = {source.internal_request_key for source in observation.sources if source.internal_request_key}
    reasons = []
    direct_sources = {source.actual_conditions["native_attempt_id"]: source for source in observation.sources
                      if source.actual_conditions.get("native_attempt_id")}
    if keys or direct_sources:
        selected = [receipt for receipt in run.internal_calls if receipt.request_key in keys]
        if len(selected) != len(keys) or any(receipt.state != "completed" for receipt in selected):
            raise ValueError("requested_observation_lacks_completed_native_source")
        for source in direct_sources.values():
            direct = _direct_native_source(store, run, store.load_artifact(source.artifact_id))
            if direct is None:
                raise ValueError("direct native observation source lost its original provenance")
            selected.append(direct)
        covered = set()
        for receipt in selected:
            actual = receipt.actual_conditions
            system_id = actual.get("system_id")
            if (actual.get("origin") != "admitted_native_input"
                    or actual.get("request_version") != request.version
                    or (system_id not in requested if requested else system_id is not None)
                    or step.system_id is not None and system_id != step.system_id):
                raise ValueError("native_input_purpose_binding_missing_or_changed")
            covered.add(system_id)
            assessment = _native_current_conditions(request, goal, system_id, policy="managed-native-1")
            reasons.extend(assessment["reasons"])
            for name, expected in assessment["conditions"].items():
                canonical = canonical_native_condition(name, expected, policy="managed-native-1")
                if canonical is None or canonical != canonical_native_condition(
                        name, actual.get(name), policy="managed-native-1"):
                    reasons.append("native_source_condition_missing_or_mismatch:" + name)
            relation, extra = _additional_conditions(request, goal, system_id, actual=actual)
            reasons.extend(extra)
            if relation is not None:
                source_observation = observation.model_copy(update={"sources": [source for source in observation.sources
                    if (source.internal_request_key == receipt.request_key if receipt.request_key is not None else
                        source.actual_conditions.get("native_attempt_id") == receipt.parent_attempt_id)]})
                _geometry_relation_evidence(store, run, request, step, attempt, source_observation,
                                            [receipt], system_id, relation)
        if requested and covered != set(requested):
            reasons.append("generic_observation_requested_system_source_missing")
        if any(source.actual_conditions.get("origin") == "registered_task_input"
               for source in observation.sources):
            # A known native source cannot lend its conditions to another raw
            # imported source used in the same derived value.
            reasons.append("mixed_input_source_conditions_unverified")
        return reasons

    # Explicit analysis of registered input files is an execution in its own
    # right. File identity does not establish ORCA conditions or qualification.
    inputs = [source for source in observation.sources
              if source.actual_conditions.get("origin") == "registered_task_input"]
    declared = set(_declared_scientific_conditions(request, goal))
    for system_id in requested:
        declared.update(name for name in systems[system_id].conditions
                        if name in {"method", "basis", "charge", "multiplicity", "electronic_state", "environment"})
    for system_id in requested or [None]:
        relation, extra = _additional_conditions(request, goal, system_id)
        reasons.extend(extra)
        if relation is not None:
            reasons.append("geometry_relation_requires_native_evidence")
    reasons.extend("scientific_condition_requires_native_evidence:" + name for name in sorted(declared))
    if (requested or request.geometry_artifact_id is not None) and not inputs:
        reasons.append("scientific_system_observation_lacks_registered_input_evidence")
    if inputs and (not run.permission.code_execution or not run.permission.artifact_writes
                   or "python.execute" not in run.permission.allowed_tools):
        reasons.append("registered_input_analysis_not_authorized")
    return reasons


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
        general = _general_admission(store, run)
        system_ids = goal.system_ids or [s.id for s in request.systems]
        system_id = step.system_id or (request.systems[0].id if len(request.systems) == 1 else None)
        if not general and system_ids and (len(system_ids) != 1 or system_id != system_ids[0]):
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
        if general:
            reasons.extend(_assess_general_sources(store, run, request, goal, step, attempt, observation))
        else:
            reasons.extend(_assess_legacy_sources(store, run, request, goal, step, attempt,
                                                 observation, system_id, conditions))
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


def _assess_legacy_sources(store, run, request, goal, step, attempt, observation, system_id, conditions):
    """Frozen older Runs retain their original delivery contract."""
    reasons = []
    try:
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
    return reasons
