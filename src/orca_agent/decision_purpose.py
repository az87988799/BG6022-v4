"""One local decision classification, shared by projection and reservation.

This is not a Run state or another lifecycle. A terminal decision can explain
an unmet goal; it does not reserve an imaginary later answer.
"""

import json
from dataclasses import dataclass

from orca_agent.applicability import CONDITION_FIELDS, effective_conditions

# These prompt versions require the same purpose and terminal snapshot guards.
# Keep older recorded requests readable without silently upgrading their contract.
DECISION_CONTRACT_PROMPT_VERSIONS = frozenset({
    "agent-json-v22", "agent-json-v23", "agent-json-v24", "agent-json-v25", "agent-json-v26", "agent-json-v27",
    "agent-json-v28", "agent-json-v29", "agent-json-v30", "agent-json-v31", "agent-json-v32", "agent-json-v33", "agent-json-v34",
    "agent-json-v35", "agent-json-v36", "agent-json-v37", "agent-json-v38", "agent-json-v39", "agent-json-v40", "agent-json-v41",
    "agent-json-v42", "agent-json-v43", "agent-json-v44", "agent-json-v45", "agent-json-v46", "agent-json-v47",
    "agent-json-v48", "agent-json-v49", "agent-json-v50", "agent-json-v51", "agent-json-v52", "agent-json-v53", "agent-json-v54", "agent-json-v55", "agent-json-v56", "agent-json-v57", "agent-json-v58", "agent-json-v59"})


@dataclass(frozen=True)
class DecisionPurpose:
    kind: str
    allowed_actions: tuple[str, ...]

    def as_dict(self):
        return {"kind": self.kind, "allowed_actions": list(self.allowed_actions)}


def classify_decision_purpose(actions, *, pending_messages=False, clarification=False):
    actions = tuple(sorted(set(actions)))
    if not actions:
        raise ValueError("decision requires at least one allowed action")
    if "normalize_request" in actions:
        kind = "normalization"
    elif clarification or actions == ("clarify",):
        kind = "clarification"
    elif not pending_messages and set(actions) <= {"stop", "clarify"}:
        kind = "terminal"
    else:
        kind = "planning"
    return DecisionPurpose(kind, actions)


def terminal_actions(request, run):
    """Only actual user unknowns justify a clarification instead of delivery."""
    from orca_agent.semantic import CONDITIONS, selectable_conditions

    communication = next((item.get("semantics", {}) for item in reversed(run.decisions)
        if item.get("request_version") == request.version and item.get("semantics")), {})
    if communication.get("awaiting_reply") and communication.get("questions"):
        return ("clarify", "stop")
    explicit_gaps = [*request.unresolved, *(gap for goal in request.goals for gap in goal.unresolved)]
    selectable = set(selectable_conditions(request))
    fields = CONDITIONS | {"system", "quantity", "geometry_relation"}
    # These are the existing intake field markers, including scoped missing
    # conditions. Optional temperature/standard state require an explicit gap;
    # their ordinary absence never creates a new question.
    if any(gap in fields or len(parts := gap.split(":")) >= 2
           and parts[0] in {"unknown", "missing", "field", "unconfirmed", "ambiguous"}
           and parts[1] in fields for gap in explicit_gaps):
        return ("clarify", "stop")
    for goal in request.goals:
        if goal.minimum_check_version in {"evidence-read-1", "knowledge-answer-1"}:
            continue
        if (goal.evidence_contract_version == "generic-observation-1"
                and goal.port in {"artifacts", "observations"}
                and not request.systems and not goal.system_ids and not request.geometry_artifact_id):
            continue
        for system_id in goal.system_ids or [None]:
            values = effective_conditions(request, goal, system_id)["conditions"]
            if any(values.get(field) is None for field in CONDITION_FIELDS if field not in selectable):
                return ("clarify", "stop")
    return ("stop",)


def prepared_authority(prepared):
    """Only literal program fields; DATA or a compressed lookalike grants nothing."""
    messages = prepared.body().get("messages", [])
    if len(messages) != 2 or messages[1].get("role") != "user":
        return {}
    try:
        wire = json.loads(messages[1]["content"])
    except (ValueError, TypeError, KeyError):
        return {}
    authority = wire.get("AUTHORITY", {}) if isinstance(wire, dict) else {}
    return authority if isinstance(authority, dict) else {}


def prepared_decision_purpose(prepared):
    value = prepared_authority(prepared).get("decision_purpose")
    if value is None:
        return None  # Historical/local transport requests have no purpose marker.
    if not isinstance(value, dict) or set(value) != {"kind", "allowed_actions"}:
        raise ValueError("invalid decision purpose")
    actions = value["allowed_actions"]
    if not isinstance(actions, list) or any(not isinstance(item, str) for item in actions):
        raise ValueError("invalid decision actions")
    purpose = classify_decision_purpose(actions,
        pending_messages=value["kind"] == "planning",
        clarification=value["kind"] == "clarification")
    if purpose.as_dict() != value:
        raise ValueError("inconsistent decision purpose")
    return purpose


def retained_read_result_ids(run):
    """Previously requested reads remain available while authoring task code.

    This selects existing local receipts only, not files or new queries. The
    context's Result-count and token bounds still apply to the selected set.
    """
    from orca_agent.tools.registry import get_tool

    if not run.permission.code_execution or not any(
            "execute_python" in get_tool(name).effects for name in run.permission.allowed_tools):
        return set()
    return {call.result_id for call in run.calls if call.result_id in run.result_ids
            and call.state == "completed"
            and get_tool(call.tool).effects == ["read_registered_artifact"]}


def settled_failed_python_attempts(run):
    """Return bound failure history even after a Plan removes its old Step.

    These receipts do not authorize a repair or make the historical Step ready.
    Keep every matching receipt; a later attempt must not erase an earlier cost
    or failure. Run budgets and the context bound limit the complete history.
    """
    return [attempt for attempt in run.attempts
            if attempt.execution_kind == "python" and attempt.state in {"failed", "timed_out"}
            and attempt.finished_at is not None and attempt.result_id in run.result_ids
            and attempt.frozen_step is not None
            and (attempt.frozen_step.id, attempt.frozen_step.logical_id, attempt.frozen_step.tool)
                == (attempt.step_id, attempt.logical_id, attempt.tool)]


def relevant_result_ids(run, plan=None, *, snapshot=None, feedback_ids=()):
    """Select by current bindings, feedback and dependencies, never recency.

    The caller loads these identities before building context. Cross-Run source
    references remain in the delivery snapshot and are not relabelled as local.
    """
    selected = set(feedback_ids)
    selected.update(retained_read_result_ids(run))
    if run.permission.code_execution:
        selected.update(attempt.result_id for attempt in settled_failed_python_attempts(run))
    from orca_agent.tools.registry import get_tool
    # Authoring receipts contain IDs/hashes, not inline code. Keep those exact
    # handles available across subsequent revisions without trusting model IDs.
    selected.update(call.result_id for call in run.calls if call.result_id and call.state == "completed"
                    and (get_tool(call.tool).check_contract.get("code_authoring")
                         or "query_external_document" in get_tool(call.tool).effects))
    selected.update(ref.result_id for ref in run.goal_evidence.values() if ref.run_id == run.id)
    for ref in (snapshot or {}).get("references", {}).values():
        if ref.get("run_id") == run.id and ref.get("result_id"):
            selected.add(ref["result_id"])
    if plan:
        completed = {item.step_id: item.result_id for item in [*run.attempts, *run.calls]
                     if item.result_id}
        for binding in plan.goal_map.values():
            if binding.evidence and binding.evidence.run_id == run.id:
                selected.add(binding.evidence.result_id)
            if binding.step_id in completed:
                selected.add(completed[binding.step_id])
        for step in plan.steps:
            if step.id in completed:
                continue
            selected.update(completed[identifier] for identifier in step.depends_on if identifier in completed)
            for ref in step.inputs.values():
                if ref.run_id in (None, run.id) and ref.result_id:
                    selected.add(ref.result_id)
    return [identifier for identifier in run.result_ids if identifier in selected]
