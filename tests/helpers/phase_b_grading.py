"""Shared evaluation classification; usage accounting is not response identity."""

import json

from orca_agent.model_usage import read_model_reply
from orca_agent.models import fingerprint
from orca_agent.store import StoreError


def classify_grade(grade, *, executed=None):
    """Known failures take precedence over every missing-evidence condition."""
    checks = [*grade.get("assertions", []), *grade.get("explanation", {}).values()]
    proposal = grade.get("proposal_review", {})
    delivery = grade.get("required_final_response_accepted", {})
    failed = (grade.get("safety_invariants_passed") is False
              or any(item.get("status") == "failed" for item in checks)
              or proposal.get("status") == "failed"
              or any(proposal.get(key) is False for key in (
                  "all_proposal_facts_passed", "semantic_review_passed"))
              or (delivery.get("required") is True
                  and (delivery.get("passed") is False or delivery.get("status") == "failed")))
    if failed:
        return "failed"
    if executed is False or grade.get("status") == "not_run":
        return "not_run"
    if (grade.get("status") == "passed" and grade.get("real_model_evidence_present") is True
            and grade.get("safety_invariants_passed") is True and not grade.get("fixture_gaps")
            and checks and all(item.get("status") == "passed" for item in checks)
            and proposal.get("status") == "passed"
            and (not delivery.get("required") or delivery.get("passed") is True)):
        return "passed"
    return "unverified"


def model_response_evidence(store, run):
    """Verify all receipts and bind accepted decisions to their own response.

    This validates persisted transport provenance, not remote attestation. An
    unknown timeout retains its receipt and budget, while a later successful
    response may support its own accepted proposal. Explicit offline receipts
    never establish real-model evidence.
    """
    records = run.model_records
    usage = {"known": sum(r.get("status") == "known" for r in records),
             "unknown": sum(r.get("status") == "unknown" for r in records)}
    missing = {"present": False, "usage": usage}
    if not records:
        return {**missing, "reason": "no model requests"}
    replies = {}
    try:
        for record in records:
            if (record.get("model") != "deepseek-flash" or record.get("sdk_version") != "2.28.0"
                    or record.get("status") not in {"known", "unknown"}
                    or not record.get("response_record_sha256")):
                raise ValueError("missing settled transport identity")
            reply, _ = read_model_reply(store, run, record)
            if record["id"] in replies:
                raise ValueError("duplicate model response identity")
            if (reply.error_category != record.get("error_category")
                    or reply.http_status != record.get("http_status")
                    or reply.response_model != record.get("response_model")):
                raise ValueError("settled model response identity differs")
            if reply.error_category is None and (reply.http_status != 200
                    or reply.response_model != "deepseek-flash"
                    or not reply.response_hash or not reply.proposal):
                raise ValueError("successful proposal lacks real response binding")
            replies[record["id"]] = reply
        accepted = [decision for decision in run.decisions
                    if decision.get("id") in replies and decision.get("action") != "rejected"]
        if not accepted:
            raise ValueError("no accepted proposal bound to a successful response")
        for decision in accepted:
            reply = replies[decision["id"]]
            proposal = reply.proposal or {}
            if decision.get("record_sha256"):
                saved = json.loads(store.path(f"runs/{run.id}/decisions/{decision['id']}.json").read_text(encoding="utf-8"))
                if (reply.error_category is not None or fingerprint(saved) != decision["record_sha256"]
                        or saved.get("id") != decision["id"] or saved.get("basis") != decision.get("basis")):
                    raise ValueError("accepted revision differs from its successful response binding")
                if proposal.get("action") in {"initial_plan", "revise_plan"}:
                    if not saved.get("plan") or saved["plan"].get("id") != decision.get("plan_id"):
                        raise ValueError("accepted model plan has no immutable plan binding")
                elif proposal.get("action") == "normalize_request":
                    from orca_agent.semantic import SemanticCandidate
                    candidate = SemanticCandidate.model_validate(proposal.get("parameters", {})).model_dump(mode="json")
                    if candidate != saved.get("semantics", {}).get("candidate"):
                        raise ValueError("accepted semantic candidate differs from response")
                elif not (proposal.get("action") == "clarify"
                          and saved.get("semantics", {}).get("kind") == "clarify"):
                    raise ValueError("accepted response does not authorize its revision type")
                continue
            if (reply.error_category is not None or proposal.get("action") != decision.get("action")
                    or proposal.get("parameters", {}) != decision.get("parameters", {})
                    or proposal.get("reason", "") != decision.get("reason", "")):
                raise ValueError("accepted proposal differs from its successful response")
        # Model decisions with a missing request cannot be hidden among local
        # program decisions, which have separately named identifiers.
        if any(str(d.get("id", "")).startswith("model_") and d.get("id") not in replies
               for d in run.decisions):
            raise ValueError("accepted model decision has no response receipt")
    except (StoreError, ValueError, KeyError, OSError) as exc:
        return {**missing, "reason": str(exc)}
    return {"present": True, "usage": usage, "accepted_proposals": len(accepted),
            "transport_failures": sum(r.error_category is not None for r in replies.values())}
