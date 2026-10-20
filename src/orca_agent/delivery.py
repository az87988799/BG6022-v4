"""Bounded goal facts derived from existing selections and applicability checks.

Rows are local projections, never persisted domain objects or new scientific
judgments. The selector reads evidence; the row builder is a pure function.
"""

from __future__ import annotations

import hashlib
import json
from copy import deepcopy

from orca_agent.applicability import effective_conditions
from orca_agent.goals import current_goal_evidence
from orca_agent.models import fingerprint, result_fingerprint_payload

DELIVERY_VERSION = "terminal-delivery-1"
MAX_DELIVERY_SNAPSHOT_BYTES = 2 * 1024 * 1024


def _digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
        separators=(",", ":"), allow_nan=False).encode("utf-8")).hexdigest()


def _source_references(value, found, depth=0):
    if depth > 16 or len(found) > 512:
        raise ValueError("delivery_source_reference_bound")
    if isinstance(value, dict):
        if isinstance(value.get("artifact_id"), str) and isinstance(value.get("sha256"), str):
            found.setdefault(value["artifact_id"], set()).add(value["sha256"])
        if isinstance(value.get("geometry_artifact_id"), str) and isinstance(value.get("geometry_sha256"), str):
            found.setdefault(value["geometry_artifact_id"], set()).add(value["geometry_sha256"])
        for key, digest in value.get("artifact_hashes", {}).items() if isinstance(value.get("artifact_hashes"), dict) else ():
            found.setdefault(key, set()).add(digest)
        for child in value.values():
            _source_references(child, found, depth + 1)
    elif isinstance(value, (list, tuple)):
        for child in value:
            _source_references(child, found, depth + 1)


def _result_bindings(value, found, depth=0):
    if depth > 16 or len(found) > 512:
        raise ValueError("delivery_result_reference_bound")
    if isinstance(value, dict):
        if all(isinstance(value.get(key), str) for key in ("run_id", "result_id", "result_fingerprint")):
            found.append({key: value[key] for key in ("run_id", "result_id", "result_fingerprint")})
        for child in value.values():
            _result_bindings(child, found, depth + 1)
    elif isinstance(value, (list, tuple)):
        for child in value:
            _result_bindings(child, found, depth + 1)


def _source_integrity(store, result, *, observation=None, code=None, port=None):
    """Check the selected output's provenance without promoting sibling data."""
    references = ({} if observation else {identifier: set() for identifier in result.artifact_ids})
    try:
        if observation:
            _source_references(observation.model_dump(mode="json"), references)
            for identifier in getattr(observation, "artifact_ids", []):
                references.setdefault(identifier, set())
            if code:
                _source_references(code.model_dump(mode="json"), references)
            manifest = result.source.get("files", {}).get("task/observations.json")
            if manifest:
                _source_references(manifest, references)
        else:
            _source_references(result.source, references)
            _source_references(result.observations, references)
            # Legacy scientific outputs retain their original whole-Result
            # evidence contract. Added generic records do not extend that
            # contract to unrelated observations' external source files.
            outputs = ([result.qualified_outputs[port]] if port in result.qualified_outputs
                       else result.qualified_outputs.values())
            for output in outputs:
                _source_references(output.source, references)
                if output.artifact_id:
                    references.setdefault(output.artifact_id, set())
        artifacts, errors, bound_results = [], [], []
        if observation is None:
            _result_bindings(result.source, bound_results)
        for binding in bound_results:
            try:
                original = store.load_result(binding["run_id"], binding["result_id"])
                if fingerprint(original) != binding["result_fingerprint"]:
                    raise ValueError("source Result fingerprint differs")
                binding["status"] = "verified"
            except (KeyError, TypeError, ValueError, OSError, RuntimeError):
                binding["status"] = "unverified"
                errors.append("source_result_unverified:" + binding["result_id"])
        for identifier, expected in sorted(references.items()):
            try:
                artifact = store.load_artifact(identifier)
                store.artifact_path(identifier)
                if expected and expected != {artifact.sha256}:
                    raise ValueError("source hash binding differs")
                artifacts.append({"artifact_id": identifier, "sha256": artifact.sha256,
                                  "status": "verified"})
            except (KeyError, TypeError, ValueError, OSError, RuntimeError):
                errors.append("source_unverified:" + identifier)
                artifacts.append({"artifact_id": identifier, "status": "unverified"})
        return {"status": "unverified" if errors else "verified", "artifacts": artifacts,
                "results": bound_results, "gaps": errors}
    except (KeyError, TypeError, ValueError, OSError, RuntimeError):
        return {"status": "unverified", "artifacts": [], "gaps": ["source_bindings_unreadable"]}


def collect_goal_selections(store, run, request, plan=None, results=None):
    """The sole current-purpose selector, with separate read-time integrity facts."""
    selections = {}
    whole_integrity = {}
    for goal in request.goals:
        selection = current_goal_evidence(store, run, request, goal, plan, results)
        if selection.get("result"):
            result = selection["result"]
            observed, code = None, None
            if goal.evidence_contract_version == "generic-observation-1":
                matches = [item for item in result.observation_records if item.name == goal.port]
                observed = matches[0] if len(matches) == 1 else None
                attempt = next((item for item in run.attempts if item.id == result.attempt_id), None)
                code = getattr(attempt.frozen_step.parameters, "code", None) if attempt and attempt.frozen_step else None
            if (_continuation_policy(run) and observed is None and goal.port not in result.qualified_outputs
                    and result.id in whole_integrity):
                selection["source_integrity"] = deepcopy(whole_integrity[result.id])
            else:
                selection["source_integrity"] = _source_integrity(store, result, observation=observed, code=code,
                                                                  port=goal.port)
                if _continuation_policy(run) and observed is None and goal.port not in result.qualified_outputs:
                    whole_integrity[result.id] = selection["source_integrity"]
            if _continuation_policy(run):
                selection["continuation_evidence"] = _continuation_evidence(
                    store, run, request, plan, goal, result, observed)
        if _continuation_policy(run):
            selection["analysis_source_candidates"] = _current_analysis_sources(
                store, run, request, plan, goal, selection, whole_integrity)
        selections[goal.id] = selection
    return selections


def _value(value, *, limit=2048):
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    if len(raw.encode("utf-8")) <= limit:
        return value
    return {"projection": "reference", "sha256": hashlib.sha256(raw.encode("utf-8")).hexdigest(),
            "bytes": len(raw.encode("utf-8")), "meaning": "full value in source Result"}


def goal_fact_rows(request, run, selections):
    """Render current purpose, source facts and eligible answers without I/O."""
    from orca_agent.tools.registry import catalog

    input_ports = {port for tool in catalog() if "write_input_artifact" in tool["effects"]
                   for port in tool["output_ports"]}
    rows = []
    for goal in request.goals:
        selection = selections.get(goal.id, {})
        result = selection.get("result")
        assessment = selection.get("assessment") or {}
        gaps = list(dict.fromkeys([*request.unresolved, *goal.unresolved,
                                 *selection.get("gaps", []), *assessment.get("reasons", []),
                                 *selection.get("source_integrity", {}).get("gaps", [])]))
        applicable = assessment.get("status") == "passed" and not gaps
        output = result.qualified_outputs.get(goal.port) if result and applicable else None
        observation = result.observations.get(goal.port) if result else None
        if result and goal.evidence_contract_version == "generic-observation-1":
            matches = [item for item in result.observation_records if item.name == goal.port]
            observation = matches[0].model_dump(mode="json") if len(matches) == 1 else None
        systems = goal.system_ids or ([request.systems[0].id] if len(request.systems) == 1 else [None])
        current = ({identifier or "request": effective_conditions(request, goal, identifier)
                    for identifier in systems}
                   if goal.minimum_check_version not in {"evidence-read-1", "knowledge-answer-1"} else {})
        current_use = assessment.get("current_use") or {}
        source_conditions = current_use.get("source_conditions") or (result.source.get("conditions", {}) if result else {})
        source = ({"run_id": result.run_id, "result_id": result.id, "attempt_id": result.attempt_id,
                   "call_id": result.call_id, "artifact_ids": result.artifact_ids} if result else None)
        if source:
            source = {key: value for key, value in source.items() if value is not None}
        answer = None
        if output:
            answer = {"kind": "qualified_input_artifact" if goal.port in input_ports else "qualified_scientific_output",
                      "value": _value(output.value),
                      "unit": output.unit, "artifact_id": output.artifact_id,
                      "check_versions": sorted({check.rule_version for check in output.checks})}
            if goal.port in input_ports:
                answer["scientific_qualification"] = False
        elif applicable and (goal.minimum_check_version in {"evidence-read-1", "knowledge-answer-1"}
                or goal.evidence_contract_version == "generic-observation-1") and observation is not None:
            # The read Tool already bounds returned lines/elements/bytes. The
            # requested answer cannot be replaced by a hash: actual matches,
            # values, units and partial-read facts are needed for delivery.
            # Oversized terminal contexts fail preflight instead of erasing it.
            observed_value = deepcopy(observation)
            answer = {"kind": "evidence_observation", "observation": observed_value,
                      "unit": observation.get("unit", observation.get("units")) if isinstance(observation, dict) else None,
                      "scientific_qualification": False, "check_versions": [goal.minimum_check_version]}
        recorded = run.goal_status.get(goal.id, "insufficient_evidence")
        source_geometry = result.source.get("geometry_artifact_id") if result else None
        if applicable and result and (goal.port == "optimized_geometry" or
                goal.conditions.get("geometry_relation") == "optimized"):
            optimized = result.qualified_outputs.get("optimized_geometry")
            if optimized:
                source_geometry = optimized.artifact_id
        row = {"goal_id": goal.id, "port": goal.port, "required": goal.required,
                     "system_ids": goal.system_ids, "requested_identity": goal.identity,
                     "current_conditions": {key: item["conditions"] for key, item in current.items()},
                     "condition_sources": {key: item["sources"] for key, item in current.items()},
                     "source_conditions": source_conditions,
                     "source_condition_evidence": current_use.get("source_condition_evidence", {}),
                     "geometry_relation": goal.conditions.get("geometry_relation"),
                     "requested_geometry_artifact_ids": [system.geometry_artifact_id for system in request.systems
                         if system.id in systems] or ([request.geometry_artifact_id] if request.geometry_artifact_id else []),
                     "source_geometry_artifact_id": source_geometry,
                     "recorded_status": recorded, "current_evidence_status": "passed" if applicable else "unresolved",
                     "goal_complete": recorded == "satisfied" and applicable,
                     "answer": answer, "source": source,
                     "minimum_check_version": goal.minimum_check_version,
                     "minimum_evidence": assessment.get("minimum_evidence", []),
                     "gaps": gaps or ([] if applicable else ["qualified_current_goal_evidence_missing"]),
                     "next_action": "deliver" if applicable else "resolve_gaps_in_scope"}
        if assessment.get("verification_basis") == "reported_delivery":
            row.update(verification_basis="reported_delivery", scientific_qualification=False,
                goal_completion_basis="reported_delivery",
                user_obligations_verified=all(entry.get("status") == "passed"
                    for entry in assessment.get("minimum_evidence", [])))
        for key in ("system_ids", "requested_identity", "source_condition_evidence", "minimum_evidence",
                    "requested_geometry_artifact_ids", "condition_sources"):
            if row[key] in ({}, []):
                row.pop(key)
        if goal.minimum_check_version in {"evidence-read-1", "knowledge-answer-1"}:
            for key in ("geometry_relation", "source_geometry_artifact_id"):
                if row[key] is None:
                    row.pop(key)
        rows.append(row)
    return rows


def collect_goal_facts(store, run, request, plan=None, results=None):
    """Use the shared Store-backed goal selection, then build pure bounded rows."""
    return goal_fact_rows(request, run, collect_goal_selections(store, run, request, plan, results))


def delivery_communication(run):
    item = next((item["semantics"] for item in reversed(run.decisions)
        if item.get("request_version") == run.request_version
        and "delivery_scope" in item.get("semantics", {})), {})
    return {key: deepcopy(item.get(key, default)) for key, default in (
        ("delivery_scope", "science"), ("awaiting_reply", False), ("notices", []),
        ("questions", []), ("question_gaps", {}))}


def _science_resources(run):
    # Model settlement, decision counters and the clock cannot invalidate the
    # very explanation whose HTTP reservation/settlement changed those values.
    permission = run.permission.model_dump(mode="json")
    # False is the legacy absence of task-code permission; retain the v1 digest.
    # An actual new grant must still change the immutable delivery snapshot.
    if not run.permission.code_execution:
        permission.pop("code_execution", None)
    resources = {"permission": permission,
            "remaining": {name: max(0, getattr(run.budget, name) - getattr(run.usage, counter))
                for name, counter in (("orca_starts", "orca_starts_reserved"),
                    ("extra_orca_starts", "extra_orca_starts_reserved"),
                    ("evidence_reads", "evidence_reads"), ("analysis_executions", "analysis_executions"),
                    ("identity_queries", "identity_queries"), ("structure_preparations", "structure_preparations"), ("knowledge_queries", "knowledge_queries"))},
            "limits": {name: getattr(run.budget, name) for name in (
                "orca_starts", "extra_orca_starts", "evidence_reads", "analysis_executions",
                "identity_queries", "structure_preparations", "knowledge_queries")},
            "usage": {name: getattr(run.usage, name) for name in (
                "orca_starts_reserved", "orca_starts_actual", "extra_orca_starts_reserved",
                "evidence_reads", "analysis_executions", "identity_queries", "structure_preparations", "knowledge_queries")},
            "unsettled_attempt_ids": [item.id for item in run.attempts
                if item.state in {"intent", "running", "unknown"}],
            "unsettled_call_ids": [item.id for item in run.calls if item.state in {"reserved", "unknown"}]}
    if run.budget.file_writes is not None:
        resources["limits"]["file_writes"] = run.budget.file_writes
        resources["usage"]["file_writes"] = run.usage.file_writes
        resources["remaining"]["file_writes"] = max(0, run.budget.file_writes - run.usage.file_writes)
    managed = (run.permission.code_execution or "orca.run" in run.permission.allowed_tools
               or run.internal_calls or any(item.execution_kind == "python" for item in run.attempts))
    if managed:
        # New execution facts must be visible at the final decision too. Keep the
        # historical terminal snapshot unchanged when this contract was unused.
        for name in ("internal_work_units", "code_retries", "postprocess_starts"):
            resources["limits"][name] = getattr(run.budget, name)
            resources["usage"][name] = getattr(run.usage, name)
            resources["remaining"][name] = max(0, getattr(run.budget, name) - getattr(run.usage, name))
        from orca_agent.planning import additional_code_attempts_used
        effective = additional_code_attempts_used(run)
        resources["code_attempt_allowance"] = {"recorded": run.usage.code_retries,
            "effective_used": effective,
            "basis": "max(recorded, python_attempt_count - 1)",
            "meaning": "every Python execution after the first, including distinct scripts"}
        resources["remaining"]["code_retries"] = max(0, run.budget.code_retries - effective)
        resources["unsettled_internal_request_keys"] = [item.request_key for item in run.internal_calls
            if item.state in {"reserved", "running", "unknown"}]
        resources["execution_cost"] = {
            "python_attempts": sum(item.execution_kind == "python" for item in run.attempts),
            "cpu_seconds": run.usage.cpu_seconds,
            "elapsed_seconds": run.usage.elapsed_seconds,
            "complete": run.usage.resource_usage_complete,
        }
        resources["artifact_limits"] = {name: getattr(run.budget, name) for name in (
            "artifact_count", "artifact_bytes", "artifact_total_bytes")}
        # The immutable import ledger lives in Store, not BudgetUsage. The
        # pure snapshot cannot manufacture its cumulative totals or disk quota.
        resources["artifact_usage"] = {"status": "not_in_snapshot", "disk_hard_quota": False}
    return resources


def _criterion_facts(result):
    """Copy saved Tool predicates and operands; never re-run scientific judgment."""
    analysis = result.observations.get("analysis")
    if not isinstance(analysis, dict):
        return None
    fields = ("operation_status", "rule_version", "scientific_status", "goal_satisfied", "reason",
              "acceptance_criteria", "target_width_angstrom", "energy_threshold_eh",
              "neighbor_span_angstrom", "left_gap_eh", "right_gap_eh", "minimum_candidate_id",
              "observed_lowest_candidate_id", "left_neighbor_id", "right_neighbor_id", "limitation",
              "quantity", "formula", "unit", "checks", "assessment_status", "predicate_satisfied",
              "predicate_rule_version", "predicate_checks")
    return {key: deepcopy(analysis[key]) for key in fields if key in analysis}


def _existing_evidence_repair(request, run, goal, selection, resources):
    """Describe a possible repair; the original Tool still validates every source."""
    from orca_agent.tools.registry import call_usage_counter, get_tool

    write_counter = call_usage_counter(get_tool("workspace.write"), run.budget)
    missing_source = {"requested_observation_lacks_completed_native_source",
                      "scientific_system_observation_lacks_native_evidence",
                      "scientific_geometry_observation_lacks_native_evidence"}
    reasons = set((selection.get("assessment") or {}).get("reasons", []))
    harmless_purpose_gap = {"minimum_evidence_missing_failed_or_wrong_version:purpose_preserved@1",
                           "minimum_evidence_missing_failed_or_wrong_version:original purpose and conditions must be preserved"}
    permission = run.permission
    result = selection.get("result")
    if (goal.evidence_contract_version != "generic-observation-1"
            or goal.verification != "reported_with_limitations" or not reasons.intersection(missing_source)
            or reasons - missing_source - harmless_purpose_gap
            or result is None or result.operation_status != "completed"
            or selection.get("source_integrity", {}).get("status") != "verified"
            or not permission.code_execution or not permission.artifact_writes
            or not {"workspace.write", "python.execute"} <= set(permission.allowed_tools)
            or resources["remaining"].get(write_counter, 0) <= 0
            or resources["remaining"].get("code_retries", 0) <= 0
            or any(resources.get(name) for name in ("unsettled_attempt_ids", "unsettled_call_ids",
                                                   "unsettled_internal_request_keys"))):
        return None
    systems = goal.system_ids or [system.id for system in request.systems]
    if len(systems) > 1:
        return None
    system_id = systems[0] if systems else None
    parents = {attempt.id: attempt for attempt in run.attempts}
    sources = []
    for call in run.internal_calls:
        parent = parents.get(call.parent_attempt_id)
        if (call.state != "completed" or call.finished_at is None or not call.response or parent is None
                or parent.state != "completed" or parent.finished_at is None
                or parent.request_version != request.version
                or call.actual_conditions.get("request_version") != request.version
                or call.actual_conditions.get("system_id") != system_id):
            continue
        files = {name: deepcopy(reference) for name, reference in call.response.get("files", {}).items()
                 if name == "stdout.out" and isinstance(reference, dict) and reference.get("artifact_id") in call.artifact_ids
                 and isinstance(reference.get("sha256"), str)}
        if files:
            sources.append({"internal_request_key": call.request_key, "source_attempt_id": parent.id,
                "files": files})
    if not sources:
        return None
    return {"source_candidates": sources,
            write_counter + "_remaining": resources["remaining"][write_counter],
            "code_attempts_remaining": resources["remaining"]["code_retries"],
            "new_orca_starts_required": 0,
            "validation": "Planning suggestion only; source hashes, Request binding and minimum evidence must still pass the existing Tool checks."}


def _continuation_policy(run):
    return bool(run.configuration
                and run.configuration.observation_delivery_policy == "reported-observation-2")


def _current_records(run, request, plan):
    from orca_agent.decision_purpose import current_step_execution_records

    if not plan or (plan.id, plan.version, plan.request_id, plan.request_version) != (
            run.plan_id, run.plan_version, request.id, request.version):
        return {}
    records = {}
    for step, record in current_step_execution_records(run, plan):
        if record.request_version == request.version:
            records[step.id] = record
    return records


def _current_analysis_sources(store, run, request, plan, goal, selection, integrity_cache):
    """Current raw files may support authoring, without becoming a Goal answer.

    A structured Plan gap is not evidence that computation is still needed.
    Neither its free text nor any task-authored file grants execution rights.
    """
    if (not goal.required or goal.evidence_contract_version != "generic-observation-1"
            or goal.verification != "reported_with_limitations" or not plan):
        return []
    binding = plan.goal_map.get(goal.id)
    if not binding or binding.port != goal.port:
        return []
    selected = selection.get("result")
    selected_execution = selection.get("continuation_evidence")
    rejected_analysis = (selected is not None and selected.operation_status == "completed"
        and selection.get("source_integrity", {}).get("status") == "verified"
        and selected_execution is not None
        and not any(call["state"] != "completed" for call in selected_execution["internal_calls"])
        and {"requested_observation_missing", "generic_observation_differs_from_archived_manifest"}.intersection(
            (selection.get("assessment") or {}).get("reasons", []))
        and not any(item.name == goal.port for item in selected.observation_records)
        and any(entry.get("category") == "observation_manifest_rejected" and entry.get("name") == goal.port
                for entry in selected.diagnostics))
    if not binding.gap and not rejected_analysis:
        return []
    systems = goal.system_ids or [item.id for item in request.systems]
    if len(systems) != 1 or systems[0] not in {item.id for item in request.systems}:
        return []
    sources = []
    for record in _current_records(run, request, plan).values():
        step = record.frozen_step
        system = step.system_id or (request.systems[0].id if len(request.systems) == 1 else None)
        if (system != systems[0] or getattr(record, "execution_kind", None) not in {"orca", "python"}
                or record.state != "completed" or record.finished_at is None
                or record.result_id not in run.result_ids
                or run.selected_results.get(record.step_id) != record.result_id
                or any(call.parent_attempt_id == record.id and call.state != "completed"
                       for call in run.internal_calls)):
            continue
        try:
            result = store.load_result(run.id, record.result_id)
            if ((result.run_id, result.attempt_id, result.step_id) != (run.id, record.id, record.step_id)
                    or result.operation_status != "completed"
                    or result.source.get("request_version") != request.version
                    or result.observations.get("evidence_collection_complete") is not True):
                continue
            code = getattr(step.parameters, "code", None) or getattr(step.parameters, "input", None)
            artifact = store.load_artifact(code.artifact_id)
            store.artifact_path(artifact.id)
            if artifact.sha256 != code.sha256 or result.source.get("code_sha256") != code.sha256:
                continue
            integrity = integrity_cache.get(result.id)
            if integrity is None:
                integrity = _source_integrity(store, result)
                integrity_cache[result.id] = integrity
            if integrity["status"] != "verified":
                continue
            files = {}
            for name, reference in result.source.get("files", {}).items():
                if not name.startswith("task/") or not isinstance(reference, dict):
                    continue
                raw = store.load_artifact(reference["artifact_id"])
                if ((raw.run_id, raw.attempt_id) != (run.id, record.id)
                        or raw.id not in result.artifact_ids or raw.sha256 != reference.get("sha256")
                        or raw.source.get("task_path") != name):
                    raise ValueError("current task file provenance differs")
                if raw.size:
                    files[name] = {"artifact_id": raw.id, "sha256": raw.sha256}
            if files:
                file_view = _value(files, limit=1536)
                if file_view.get("projection") == "reference":
                    # Keep one immediately readable handle when the complete
                    # index stays in the referenced Result. A stream is an
                    # inspection candidate, never evidence of a physical value.
                    name = "task/stdout.out" if "task/stdout.out" in files else next(iter(files))
                    file_view["inspection_file"] = {"name": name, **files[name]}
                sources.append({"run_id": run.id, "result_id": result.id,
                    "result_fingerprint": _digest(result_fingerprint_payload(result, exclude={"created_at"})),
                    "attempt_id": record.id, "step_id": record.step_id, "system_id": system,
                    "request_version": request.version, "source_integrity": "verified",
                    "files": file_view, "files_field": "source.files"})
        except (AttributeError, KeyError, TypeError, ValueError, OSError, RuntimeError):
            continue
    if rejected_analysis and not any(source["result_id"] == selected.id
            and source["result_fingerprint"] == _digest(result_fingerprint_payload(selected, exclude={"created_at"}))
            for source in sources):
        # A good neighboring native Result cannot authenticate a damaged or
        # foreign analysis Result's rejection. Both retain their own lineage.
        return []
    return sources


def _continuation_evidence(store, run, request, plan, goal, result, observed):
    """Authenticate current producer diagnostics; task declarations grant no rights."""
    record = _current_records(run, request, plan).get(result.step_id)
    if (not record or record.result_id != result.id or result.run_id != run.id
            or result.id not in run.result_ids or record.state != result.operation_status
            or getattr(record, "finished_at", None) is None
            or result.attempt_id != record.id):
        return None
    names = getattr(record.frozen_step.parameters, "observation_names", [])
    if goal.port not in names:
        return None
    code = getattr(record.frozen_step.parameters, "code", None) or getattr(record.frozen_step.parameters, "input", None)
    try:
        artifact = store.load_artifact(code.artifact_id)
        store.artifact_path(artifact.id)
        if artifact.sha256 != code.sha256:
            return None
    except (AttributeError, KeyError, ValueError, OSError, RuntimeError):
        return None
    keys = {source.internal_request_key for source in observed.sources
            if source.internal_request_key} if observed else set()
    calls, diagnostics = [], []
    for call in run.internal_calls:
        if (call.parent_attempt_id != record.id or call.finished_at is None
                or call.state not in {"completed", "failed", "cancelled", "timed_out"}
                or call.actual_conditions.get("request_version") != request.version
                or goal.system_ids and call.actual_conditions.get("system_id") not in goal.system_ids
                or keys and call.request_key not in keys):
            continue
        # A completed explicit retry replaces the failed source for continuation;
        # never demand another calculation because an old receipt is retained.
        if any(other.retry_of == call.request_key and other.state == "completed"
               and other.finished_at is not None for other in run.internal_calls):
            continue
        files = {}
        for name, ref in (call.response or {}).get("files", {}).items():
            if name not in {"stdout.out", "stderr.txt"} or not isinstance(ref, dict):
                continue
            try:
                artifact = store.load_artifact(ref["artifact_id"])
                store.artifact_path(artifact.id)
                if (artifact.id not in call.artifact_ids or artifact.sha256 != ref.get("sha256")
                        or (artifact.run_id, artifact.attempt_id) != (run.id, record.id)):
                    return None
                files[name] = {"artifact_id": artifact.id, "sha256": artifact.sha256}
            except (KeyError, ValueError, OSError, RuntimeError):
                return None
        if not files:
            continue
        calls.append({"request_key": call.request_key, "state": call.state,
            "work_units": call.work_units, "files": files})
        for entry in result.diagnostics:
            if entry.get("request_key") != call.request_key:
                continue
            for evidence in entry.get("evidence", []):
                if any(evidence.get("artifact_id") == ref["artifact_id"]
                       and evidence.get("sha256") == ref["sha256"] for ref in files.values()):
                    diagnostics.append({key: deepcopy(evidence[key]) for key in
                        ("filename", "artifact_id", "sha256", "byte_start", "total_bytes") if key in evidence}
                        | {"text": evidence.get("text", "")[:512]})
    admission = [entry for entry in result.diagnostics if entry.get("category") == "observation_admission"
                 or entry.get("category") == "observation_manifest_rejected" and entry.get("name") == goal.port]
    return {"attempt_id": record.id, "step_id": record.step_id, "logical_id": record.logical_id,
        "state": record.state, "execution_kind": record.execution_kind,
        "internal_calls": calls, "diagnostics": diagnostics or _value(result.diagnostics, limit=2048),
        "observation_admission": _value(admission, limit=2048) if admission else []}


def _unsettled(resources):
    return any(resources.get(name) for name in ("unsettled_attempt_ids", "unsettled_call_ids",
                                               "unsettled_internal_request_keys"))


def _work_budget(run, steps, resources):
    """Check necessary declared effects only; admission still checks exact inputs."""
    from collections import Counter

    from orca_agent.tools.registry import call_usage_counter, get_tool, require_execution_available

    required = Counter()
    permission = run.permission
    for step in steps:
        try:
            tool = get_tool(step.tool)
            require_execution_available(step.tool)
        except ValueError:
            return False
        effects = set(tool.effects)
        if step.tool not in permission.allowed_tools:
            return False
        for effect, granted in (("execute_orca", permission.scientific_execution),
                ("execute_python", permission.code_execution), ("prepare_geometry", permission.geometry_preparation),
                ("query_external_identity", permission.external_identity_queries)):
            if effect in effects and not granted:
                return False
        if effects & {"write_artifact", "write_analysis", "write_input_artifact", "import_artifact"} and not permission.artifact_writes:
            return False
        if "execute_python" in effects:
            required["analysis_executions"] += 1
            if run.usage.logical_attempts.get(step.logical_id, 0):
                required["code_retries"] += 1
        counter = call_usage_counter(tool, run.budget)
        if counter:
            required[counter] += 1
        sdk = getattr(step.parameters, "sdk_orca", False)
        if sdk and "orca.run" not in permission.allowed_tools:
            return False
        if "execute_orca" in effects or sdk:
            if not permission.scientific_execution:
                return False
            required["orca_starts"] += 1
            extra = (bool(run.usage.orca_starts_reserved or run.internal_calls
                          or any(item.execution_kind == "orca" for item in run.attempts)) if sdk else
                     bool(run.internal_calls or run.usage.logical_attempts.get(step.logical_id, 0)
                          or run.initial_science_steps is not None and step.logical_id not in run.initial_science_steps))
            if extra:
                if not permission.allow_additional_science:
                    return False
                required["extra_orca_starts"] += 1
            if sdk:
                required["internal_work_units"] += 1
        if run.usage.logical_attempts.get(step.logical_id, 0) >= run.budget.attempts_per_step:
            return False
    if required["extra_orca_starts"]:
        from orca_agent.store import _extra_orca_reservations_used
        try:
            if run.budget.extra_orca_starts - _extra_orca_reservations_used(run) < required["extra_orca_starts"]:
                return False
        except ValueError:
            return False
    return all(resources["remaining"].get(name, 0) >= count for name, count in required.items())


def _approved_goal_work(request, run, plan, goal, resources):
    if not plan or (plan.id, plan.version, plan.request_id, plan.request_version) != (
            run.plan_id, run.plan_version, request.id, request.version):
        return None
    binding = plan.goal_map.get(goal.id)
    if not binding or not binding.step_id or binding.port != goal.port:
        return None
    steps = {step.id: step for step in plan.steps}
    records = _current_records(run, request, plan)
    needed, visited = [], set()

    def visit(identifier):
        if identifier in visited:
            return True
        visited.add(identifier)
        step = steps.get(identifier)
        if not step:
            return False
        record = records.get(identifier)
        if record:
            return (record.state == "completed" and bool(record.result_id)
                    and record.result_id in run.result_ids)
        if not all(visit(dependency) for dependency in step.depends_on):
            return False
        needed.append(step)
        return True

    if not visit(binding.step_id) or not needed or not _work_budget(run, needed, resources):
        return None
    return {"kind": "execute_approved_steps", "step_ids": [step.id for step in needed],
            "goal_step_id": binding.step_id,
            "validation": "Continue the approved dependency order through the existing admission checks; no Plan revision or new scientific target is implied."}


def _goal_continuation(request, run, plan, goal, selection, resources):
    if (not goal.required or request.unresolved or goal.unresolved or _unsettled(resources)):
        return None
    pending = _approved_goal_work(request, run, plan, goal, resources)
    if pending:
        return pending
    candidates = selection.get("analysis_source_candidates")
    if candidates:
        from orca_agent.models import Step

        logical = set(run.usage.logical_steps) | {step.logical_id for step in plan.steps}
        writer = Step(id="continuation_writer", logical_id="continuation_writer", tool="workspace.write",
            parameters={"name": "analysis.py", "role": "python", "content": ""})
        analysis = Step(id="continuation_analysis", logical_id="continuation_analysis", tool="python.execute",
            parameters={"code": {"artifact_id": "continuation_code", "sha256": "0" * 64},
                        "sdk_orca": False})
        if (run.usage.plan_revisions < run.budget.plan_revisions
                and len(logical) + 2 <= run.budget.logical_steps
                and _work_budget(run, [writer, analysis], resources)):
            work = {"kind": "plan_analysis_from_current_evidence", "route": "new_analysis_step",
                "source_candidates": candidates, "new_orca_starts_required": 0,
                "validation": "Inspect the pinned current files, then explicitly propose a writer and sdk_orca=false analysis Step. File existence does not establish the requested quantity, conditions or scientific qualification; preserve the Goal and apply all existing source and evidence checks."}
            selected = selection.get("result")
            if selected:
                work["rejected_result_id"] = selected.id
                work["diagnostics_field"] = "diagnostics"
                work["admission_diagnostics"] = list({entry.get("detail", "")[:512]: {
                    "category": "observation_manifest_rejected", "detail": entry.get("detail", "")[:512]}
                    for entry in selected.diagnostics
                    if entry.get("category") == "observation_manifest_rejected"
                    and isinstance(entry.get("detail"), str)}.values())
            return work
    evidence = selection.get("continuation_evidence")
    if not evidence or selection.get("source_integrity", {}).get("status") != "verified":
        return None
    from orca_agent.models import Step

    # New code and its explicit execution use the existing bounded Plan surface.
    logical = set(run.usage.logical_steps) | {step.logical_id for step in plan.steps}
    failed_outer = evidence["state"] in {"failed", "timed_out", "cancelled"}
    if (run.usage.plan_revisions >= run.budget.plan_revisions
            or len(logical) + (1 if failed_outer else 2) > run.budget.logical_steps):
        return None
    failed = [call for call in evidence["internal_calls"] if call["state"] in {"failed", "timed_out", "cancelled"}]
    completed = [call for call in evidence["internal_calls"] if call["state"] == "completed"]
    missing_source = {"requested_observation_missing", "requested_observation_lacks_completed_native_source",
        "scientific_system_observation_lacks_native_evidence", "scientific_geometry_observation_lacks_native_evidence",
        "generic_observation_differs_from_archived_manifest"}
    reasons = set((selection.get("assessment") or {}).get("reasons", []))
    analysis = bool(not failed and not failed_outer and completed and reasons.intersection(missing_source))
    if not failed and not failed_outer and not analysis:
        return None
    original = next(step for step in plan.steps if step.id == evidence["step_id"])
    native = evidence["execution_kind"] == "orca"
    sdk = bool(failed or failed_outer and not completed and getattr(original.parameters, "sdk_orca", False))
    writer = Step(id="continuation_writer", logical_id="continuation_writer", tool="workspace.write",
                  parameters={"name": "repair.inp" if native else "repair.py",
                              "role": "orca" if native else "python", "content": ""})
    followup = original.model_copy(deep=True)
    followup.logical_id = original.logical_id if failed_outer else "continuation_analysis"
    if not native:
        followup.parameters = followup.parameters.model_copy(update={"sdk_orca": sdk})
    if not _work_budget(run, [writer, followup], resources):
        return None
    if failed and (not run.permission.allow_additional_science
                or resources["remaining"]["extra_orca_starts"] < len(failed)
                or resources["remaining"]["orca_starts"] < len(failed)
                or resources["remaining"].get("internal_work_units", 0) < sum(call["work_units"] for call in failed)):
        return None
    return {"kind": "repair_existing_evidence" if analysis else "repair_failed_execution",
        "source_attempt_id": evidence["attempt_id"], "source_step_id": evidence["step_id"],
        "outer_operation_status": evidence["state"], "internal_calls": completed if analysis else failed,
        "diagnostics": evidence["diagnostics"], "observation_admission": evidence["observation_admission"],
        "route": "repair_of_failed_outer" if failed_outer else "new_followup_step",
        "new_orca_starts_required": 0 if analysis else max(len(failed), int(sdk)) + int(native),
        "validation": "Explicit revised code/Plan and existing execution, retry, source and scientific checks remain mandatory."}


def delivery_snapshot(request, run, selections, *, plan=None, results=None, allowed_actions=(), control_generation=None):
    """Pure versioned facts/relations for Context, contract validation and report.

    Selections must come from collect_goal_selections. Short refs are scoped to
    this basis/fingerprint, not durable evidence identities. Full identities and
    hashes stay in references; no timestamp or model cost enters the fingerprint.
    """
    rows = goal_fact_rows(request, run, selections)
    communication = delivery_communication(run)
    resources = _science_resources(run)
    snapshot = {"version": DELIVERY_VERSION, "snapshot_ref": "current",
        "basis": {"request_id": request.id, "request_version": request.version,
                  "plan_id": run.plan_id, "plan_version": run.plan_version,
                  "permission_version": run.permission.version,
                  "control_generation": run.control_generation if control_generation is None else control_generation},
        "request_text": request.original_text, "communication": communication, "resources": resources,
        "goals": [], "facts": [], "blockers": [], "explanations": [], "next_actions": [], "references": {}}

    def fact(goal_ref, kind, value, source_ref=None):
        ref = "f" + str(len(snapshot["facts"]) + 1)
        snapshot["facts"].append({"ref": ref, "goal_ref": goal_ref, "kind": kind,
                                  "value": deepcopy(value), "source_ref": source_ref})
        return ref

    for index, (goal, row) in enumerate(zip(request.goals, rows, strict=True), 1):
        goal_ref = "g" + str(index)
        selection = selections.get(goal.id, {})
        result = selection.get("result")
        integrity = selection.get("source_integrity", {"status": "not_checked", "artifacts": [], "gaps": []})
        source_ref = None
        if result:
            source_ref = "s" + str(index)
            snapshot["references"][source_ref] = {**row["source"], "integrity": integrity,
                "result_fingerprint": _digest(result_fingerprint_payload(result, exclude={"created_at"})),
                "rule_versions": sorted({check.rule_version for checks in result.checks.values() for check in checks})}
        refs = [fact(goal_ref, "goal_status", {"recorded": row["recorded_status"],
                "complete": row["goal_complete"], "current_evidence": row["current_evidence_status"],
                "gaps": row["gaps"]}),
            fact(goal_ref, "conditions", {"current": row["current_conditions"],
                "sources": row.get("condition_sources", {}), "source": row["source_conditions"],
                "source_evidence": row.get("source_condition_evidence", {}),
                "geometry_relation": row.get("geometry_relation")})]
        if row.get("verification_basis") == "reported_delivery":
            refs.append(fact(goal_ref, "verification_limits", {
                "basis": "reported_delivery", "scientific_qualification": False,
                "minimum_evidence": row.get("minimum_evidence", []),
                "meaning": "Sourced observations are delivered; unverified user obligations are not checked or qualified.",
            }, source_ref))
        if result:
            refs.append(fact(goal_ref, "operation", {"status": result.operation_status,
                "source_integrity": integrity["status"], "qualified_ports": sorted(result.qualified_outputs)}, source_ref))
        if result and integrity["status"] == "verified":
            refs.append(fact(goal_ref, "checks", {port: [check.model_dump(mode="json") for check in checks]
                                                 for port, checks in result.checks.items()}, source_ref))
            criteria = _criterion_facts(result)
            if criteria:
                refs.append(fact(goal_ref, "criterion", criteria, source_ref))
            analysis = result.observations.get("analysis", {})
            if isinstance(analysis, dict) and isinstance(analysis.get("members"), list):
                members = []
                for member in analysis["members"]:
                    if not isinstance(member, dict):
                        continue
                    item = {key: deepcopy(member[key]) for key in (
                        "member_id", "id", "required", "status", "energy_eh", "missing_reason") if key in member}
                    source = member.get("source") or {}
                    if isinstance(source, dict):
                        member_ref = source_ref + "m" + str(len(members) + 1)
                        snapshot["references"][member_ref] = {key: deepcopy(source[key]) for key in (
                            "run_id", "result_id", "attempt_id", "artifact_hashes", "geometry_artifact_id",
                            "geometry_sha256", "conditions", "checks", "condition_evidence") if key in source}
                        item["source_ref"] = member_ref
                    members.append(item)
                refs.append(fact(goal_ref, "members", members, source_ref))
        if row["answer"] is not None:
            refs.append(fact(goal_ref, "answer", row["answer"], source_ref))
        continuation = (_goal_continuation(request, run, plan, goal, selection, resources)
            if _continuation_policy(run) and not row["goal_complete"]
            and communication["delivery_scope"] != "registration_only" and not communication["awaiting_reply"] else None)
        repair = (_existing_evidence_repair(request, run, goal, selection, resources)
                  if not _continuation_policy(run) and not row["goal_complete"] else None)
        if continuation:
            work_ref = "work_" + _digest(continuation)[:16]
            snapshot["references"].setdefault(work_ref, deepcopy(continuation))
            summary = {"kind": continuation["kind"], "work_ref": work_ref}
            if "step_ids" in continuation:
                summary["step_ids"] = continuation["step_ids"]
            refs.append(fact(goal_ref, "current_plan_work", summary, source_ref))
        if repair:
            refs.append(fact(goal_ref, "existing_evidence_repair", repair, source_ref))
        blockers = []

        def block(code, value, text):
            ref = "b" + str(len(snapshot["blockers"]) + 1)
            snapshot["blockers"].append({"ref": ref, "goal_refs": [goal_ref], "code": code,
                                         "value": value, "text": text})
            blockers.append(ref)

        if not row["goal_complete"]:
            if row["gaps"]:
                block("current_goal_evidence_missing", row["gaps"], "Current goal evidence is incomplete or inapplicable.")
            if result and integrity["status"] != "verified":
                block("source_unverified", integrity["gaps"], "Source integrity is not currently verified; values are withheld.")
            if goal.minimum_check_version not in {"evidence-read-1", "knowledge-answer-1"}:
                if not run.permission.scientific_execution:
                    block("scientific_execution_not_authorized", False, "New scientific execution is not authorized.")
                if not run.permission.allow_additional_science:
                    block("additional_science_not_authorized", False, "Additional scientific execution is not authorized.")
                for name in ("orca_starts", "extra_orca_starts"):
                    if resources["remaining"][name] == 0:
                        block(name + "_exhausted", {"limit": resources["limits"][name], "remaining": 0},
                              name + " remaining is zero; a larger batch budget does not override this Run."
                              + (" This blocks new calculations, not an authorized analysis of existing evidence." if repair else ""))
            if resources["unsettled_attempt_ids"] or resources["unsettled_call_ids"]:
                block("execution_unsettled", {key: resources[key] for key in (
                    "unsettled_attempt_ids", "unsettled_call_ids")}, "Reconcile unsettled execution before new work.")
        if row["goal_complete"] and row.get("verification_basis") == "reported_delivery":
            relation, text, action = ("reported_observation_delivered",
                "Sourced observation delivery is complete; unverified minimum-evidence obligations remain unverified and no scientific qualification is granted.",
                "deliver")
        elif row["goal_complete"]:
            relation, text, action = "current_goal_satisfied", "Current applicable evidence satisfies this goal.", "deliver"
        elif communication["delivery_scope"] == "registration_only" and not communication["awaiting_reply"]:
            relation, text, action = ("registration_complete_science_unmet",
                "Registration is complete; this does not complete the unmet scientific goal.", "stop_after_registration")
        elif communication["awaiting_reply"] and communication["questions"]:
            relation, text, action = "current_goal_unmet", "Current goal remains unmet; the stated questions require user input.", "clarify"
        elif continuation:
            relation, text, action = ("current_goal_unmet",
                "Current required goal has concrete authorized work within its remaining execution budget; continue unless the program confirms only final delivery remains.",
                "continue_approved_plan" if continuation["kind"] == "execute_approved_steps"
                else continuation["kind"])
        elif repair:
            relation, text, action = ("current_goal_unmet",
                "Current source binding remains incomplete; archived native evidence can be reused in an explicit analysis Step without another calculation.",
                "repair_existing_evidence")
        else:
            relation, text, action = "current_goal_unmet", "Current goal remains unmet; qualified members or a completed operation do not complete it.", "stop_with_partial_results"
        if allowed_actions and action == "clarify" and "clarify" not in allowed_actions:
            action = "stop_with_partial_results"
        action_ref, explanation_ref = "n" + str(index), "e" + str(index)
        continue_text = None
        if continuation:
            if continuation["kind"] == "execute_approved_steps":
                continue_text = "Continue the listed approved Steps in dependency order through normal admission; preserve the current Goal and scientific conditions."
            elif continuation["kind"] == "repair_existing_evidence":
                continue_text = "Revise the Plan with a new writer and analysis-only follow-up Step using pinned completed raw evidence; repair the observation manifest and Goal binding without starting ORCA."
            elif continuation["kind"] == "plan_analysis_from_current_evidence":
                continue_text = "Inspect the current source candidates and propose a new writer plus an explicit sdk_orca=false analysis Step for the unchanged Goal. Bind its sourced observation to the Goal through the normal checks; do not repeat ORCA or assume the raw files already contain the required quantity."
            elif continuation["route"] == "new_followup_step":
                continue_text = "Use the exact archived failure diagnostic to revise the input/code in a new writer and follow-up Python Step. Preserve the successful outer Step; do not use repair_of on it. For each failed SDK call use a new request key with purpose='failure_retry' and retry_of equal to the listed original key. Keep all physical targets and checks."
            else:
                continue_text = "Revise the code using the archived failure, retain the failed outer Step's logical identity through repair_of, and use normal bounded retry admission without weakening the Goal or scientific checks."
        snapshot["next_actions"].append({"ref": action_ref, "kind": action, "goal_refs": [goal_ref],
            "requires": [], "text": (continue_text or "Revise the Plan if no analysis Step is ready. Use pinned task-file copies and completed native source candidates to repair the observation manifest; preserve values and conditions, update Goal bindings, and do not start ORCA."
                if repair and action == "repair_existing_evidence" else "Ask only the recorded unresolved questions."
                if action == "clarify" else continue_text or "Deliver current facts without starting additional work.")})
        snapshot["explanations"].append({"ref": explanation_ref, "goal_ref": goal_ref,
            "fact_refs": refs, "relation": relation, "text": text,
            "blocker_refs": blockers, "next_action_refs": [action_ref]})
        snapshot["goals"].append({**row, "ref": goal_ref, "original_text": goal.original_text,
            "text_evidence": deepcopy(goal.text_evidence), "requested_minimum_evidence": list(goal.minimum_evidence),
            "required_fact_refs": refs, "required_blocker_refs": blockers,
            "explanation_refs": [explanation_ref], "next_action_refs": [action_ref]})
    snapshot["fingerprint"] = _digest(snapshot)
    return snapshot


def collect_delivery_snapshot(store, run, request, plan=None, results=None, *, allowed_actions=(), control_generation=None):
    return delivery_snapshot(request, run, collect_goal_selections(store, run, request, plan, results),
                             plan=plan, results=results, allowed_actions=allowed_actions, control_generation=control_generation)
