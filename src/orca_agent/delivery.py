"""Bounded goal facts derived from existing selections and applicability checks.

Rows are local projections, never persisted domain objects or new scientific
judgments. The selector reads evidence; the row builder is a pure function.
"""

from __future__ import annotations

import hashlib
import json

from orca_agent.applicability import effective_conditions
from orca_agent.goals import current_goal_evidence


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
                                 *selection.get("gaps", []), *assessment.get("reasons", [])]))
        applicable = assessment.get("status") == "passed" and not gaps
        output = result.qualified_outputs.get(goal.port) if result and applicable else None
        observation = result.observations.get(goal.port) if result else None
        systems = goal.system_ids or ([request.systems[0].id] if len(request.systems) == 1 else [None])
        current = ({identifier or "request": effective_conditions(request, goal, identifier)
                    for identifier in systems}
                   if goal.minimum_check_version != "evidence-read-1" else {})
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
        elif applicable and goal.minimum_check_version == "evidence-read-1" and observation is not None:
            # Reader metadata already belongs to the immutable source Result.
            # Keep scalar/array values directly; large indexes/text pages stay
            # discoverable through a bounded content hash and source identity.
            observed_value = ({"value": _value(observation["value"], limit=256)}
                              if isinstance(observation, dict) and "value" in observation
                              else _value(observation, limit=512))
            answer = {"kind": "evidence_observation", "observation": observed_value,
                      "unit": observation.get("unit", observation.get("units")) if isinstance(observation, dict) else None,
                      "scientific_qualification": False, "check_versions": ["evidence-read-1"]}
        recorded = run.goal_status.get(goal.id, "insufficient_evidence")
        row = {"goal_id": goal.id, "port": goal.port, "required": goal.required,
                     "system_ids": goal.system_ids, "requested_identity": goal.identity,
                     "current_conditions": {key: item["conditions"] for key, item in current.items()},
                     "condition_sources": {key: item["sources"] for key, item in current.items()},
                     "source_conditions": source_conditions,
                     "source_condition_evidence": current_use.get("source_condition_evidence", {}),
                     "geometry_relation": goal.conditions.get("geometry_relation"),
                     "requested_geometry_artifact_ids": [system.geometry_artifact_id for system in request.systems
                         if system.id in systems] or ([request.geometry_artifact_id] if request.geometry_artifact_id else []),
                     "source_geometry_artifact_id": result.source.get("geometry_artifact_id") if result else None,
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
        if goal.minimum_check_version == "evidence-read-1":
            for key in ("geometry_relation", "source_geometry_artifact_id"):
                if row[key] is None:
                    row.pop(key)
        rows.append(row)
    return rows


def collect_goal_facts(store, run, request, plan=None, results=None):
    """Use the shared Store-backed goal selection, then build pure bounded rows."""
    return goal_fact_rows(request, run, {goal.id: current_goal_evidence(store, run, request, goal, plan, results)
                                       for goal in request.goals})
