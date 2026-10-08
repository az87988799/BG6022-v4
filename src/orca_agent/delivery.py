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
from orca_agent.models import fingerprint

DELIVERY_VERSION = "terminal-delivery-1"


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


def _source_integrity(store, result):
    """Recheck observation provenance too, even when a scientific check failed."""
    references = {identifier: set() for identifier in result.artifact_ids}
    try:
        _source_references(result.source, references)
        _source_references(result.observations, references)
        for output in result.qualified_outputs.values():
            _source_references(output.source, references)
            if output.artifact_id:
                references.setdefault(output.artifact_id, set())
        artifacts, errors, bound_results = [], [], []
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
    for goal in request.goals:
        selection = current_goal_evidence(store, run, request, goal, plan, results)
        if selection.get("result"):
            selection["source_integrity"] = _source_integrity(store, selection["result"])
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
            answer = {"kind": "qualified_scientific_output", "value": _value(output.value),
                      "unit": output.unit, "artifact_id": output.artifact_id,
                      "check_versions": sorted({check.rule_version for check in output.checks})}
        elif applicable and goal.minimum_check_version in {"evidence-read-1", "knowledge-answer-1"} and observation is not None:
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
    return {"permission": run.permission.model_dump(mode="json"),
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


def delivery_snapshot(request, run, selections, *, allowed_actions=(), control_generation=None):
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
                "result_fingerprint": _digest(result.model_dump(mode="json", exclude={"created_at"})),
                "rule_versions": sorted({check.rule_version for checks in result.checks.values() for check in checks})}
        refs = [fact(goal_ref, "goal_status", {"recorded": row["recorded_status"],
                "complete": row["goal_complete"], "current_evidence": row["current_evidence_status"],
                "gaps": row["gaps"]}),
            fact(goal_ref, "conditions", {"current": row["current_conditions"],
                "sources": row.get("condition_sources", {}), "source": row["source_conditions"],
                "source_evidence": row.get("source_condition_evidence", {}),
                "geometry_relation": row.get("geometry_relation")})]
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
                              name + " remaining is zero; a larger batch budget does not override this Run.")
            if resources["unsettled_attempt_ids"] or resources["unsettled_call_ids"]:
                block("execution_unsettled", {key: resources[key] for key in (
                    "unsettled_attempt_ids", "unsettled_call_ids")}, "Reconcile unsettled execution before new work.")
        if row["goal_complete"]:
            relation, text, action = "current_goal_satisfied", "Current applicable evidence satisfies this goal.", "deliver"
        elif communication["delivery_scope"] == "registration_only" and not communication["awaiting_reply"]:
            relation, text, action = ("registration_complete_science_unmet",
                "Registration is complete; this does not complete the unmet scientific goal.", "stop_after_registration")
        elif communication["awaiting_reply"] and communication["questions"]:
            relation, text, action = "current_goal_unmet", "Current goal remains unmet; the stated questions require user input.", "clarify"
        else:
            relation, text, action = "current_goal_unmet", "Current goal remains unmet; qualified members or a completed operation do not complete it.", "stop_with_partial_results"
        if allowed_actions and action == "clarify" and "clarify" not in allowed_actions:
            action = "stop_with_partial_results"
        action_ref, explanation_ref = "n" + str(index), "e" + str(index)
        snapshot["next_actions"].append({"ref": action_ref, "kind": action, "goal_refs": [goal_ref],
            "requires": [], "text": "Deliver current facts without starting additional work." if action != "clarify"
                                        else "Ask only the recorded unresolved questions."})
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
                             allowed_actions=allowed_actions, control_generation=control_generation)
