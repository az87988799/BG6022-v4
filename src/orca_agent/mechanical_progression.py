"""Continue an approved Plan only after verified mechanical completion.

No model proposal, inferred binding or scientific feedback is synthesized.
The normal execution boundary still authorizes and reserves the selected Step.
"""

from orca_agent.model_usage import current_basis, terminal_delivery_due, validate_delivery_margin
from orca_agent.models import fingerprint
from orca_agent.store import BudgetExceeded
from orca_agent.tools.registry import get_tool


def select_mechanical_step(store, run, plan, results, ready, *, goals_changed=False):
    """Persist a bounded deterministic selection; None retains model control."""
    if (not store.run_configuration(run).mechanical_progression or not plan or not ready
            or goals_changed or not run.agent_enabled
            or any(item.get("status") == "reserved" for item in run.model_records)
            or any(item.state in {"intent", "reserved", "running", "unknown"}
                   for item in [*run.calls, *run.attempts])):
        return None
    feedback = [identifier for identifier in run.result_ids if identifier not in run.processed_feedback]
    if not feedback:
        return None
    with store.control_lock(run.id):
        control = store.read_control(run.id)
        if (store.load_run(run.id) != run or control["action"] or control["generation"] != run.control_generation
                or any(message["id"] not in run.processed_messages for message in control["messages"])):
            return None
        basis = current_basis(store, run)
        if plan != store.load_plan(run) or plan.request_version != run.request_version:
            return None
        facts = []
        by_step = {step.id: step for step in plan.steps}
        for identifier in feedback:
            result = store.load_result(run.id, identifier)
            step = by_step.get(result.step_id)
            call = next((item for item in run.calls if item.id == result.call_id), None)
            if (step is None or call is None or call.state != "completed"
                    or call.result_id != identifier or call.frozen_step != step
                    or call.request_version != run.request_version
                    or call.plan_version != run.plan_version
                    or call.control_generation != run.control_generation
                    or result.operation_status != "completed" or result.qualified_outputs
                    or result.observation_records or result.diagnostics
                    or results.get(step.id) != result):
                return None
            tool = get_tool(step.tool)
            if (tool.check_contract.get("mechanical_feedback") != "immutable-file-write-1"
                    or "write_artifact" not in tool.effects
                    or not set(tool.effects) <= {"read_registered_artifact", "write_artifact"}
                    or set(result.observations) != set(tool.observation_outputs)
                    or set(result.checks) != set(tool.observation_outputs)):
                return None
            if any(not checks or any(check.status != "passed" or check.rule_version != tool.check_version
                                     for check in checks) for checks in result.checks.values()):
                return None
            if not result.artifact_ids:
                return None
            for artifact_id in result.artifact_ids:
                store.artifact_path(artifact_id)  # Existing content/hash verification.
            facts.append({"result_id": identifier, "fingerprint": fingerprint(result), "tool": step.tool})
        chosen = ready[0]
        if (chosen != by_step.get(chosen.id) or chosen.id in results
                or any(dep not in results or results[dep].operation_status != "completed"
                       for dep in chosen.depends_on)):
            return None
        ticket = "mechanical_" + fingerprint({"basis": basis, "facts": facts, "step": chosen.id})[:24]
        if any(item["id"] == ticket for item in run.decisions):
            return None  # Existing pending-decision recovery owns replay.
        if run.usage.decision_rounds >= run.budget.decision_rounds:
            raise BudgetExceeded("decision round budget exhausted")
        draft = run.model_copy(deep=True)
        draft.usage.decision_rounds += 1
        if terminal_delivery_due(store.load_request(run), draft):
            return None
        try:
            validate_delivery_margin(store.load_request(run), draft, purpose="planning")
        except BudgetExceeded:
            return None
        run.usage.decision_rounds += 1
        run.decisions.append({"id": ticket, "basis": basis, "action": "call_tool",
            "related_results": feedback, "parameters": {"step_id": chosen.id},
            "reason": "Verified mechanical completion; continue the unchanged approved Plan.",
            "selection": "mechanical-feedback-1", "feedback_checks": facts})
        run.processed_feedback = list(dict.fromkeys([*run.processed_feedback, *feedback]))
        store.save_run(run)
        return chosen, ticket
