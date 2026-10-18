"""Small versioned compatibility table for existing Goal evidence requirements."""

RULE_VERSION = "minimum-evidence-1"
LEGACY_NAMES = {
    "original purpose and conditions must be preserved": "purpose_preserved@1",
    "converged SCF": "converged_scf@1",
    "converged_scf": "converged_scf@1",
    "all_initial_members": "all_initial_members@1",
}
REQUIREMENTS = {
    "purpose_preserved@1": {"ports": None, "predicate": "current_use_passed"},
    "converged_scf@1": {"ports": ("energy", "optimized_geometry", "dipole_moment"), "check": "scf_converged"},
    "all_initial_members@1": {"ports": ("sampling", "sampling_check", "member_table", "energy_difference"),
                              "predicate": "required_members_qualified"},
}


def describe_evidence_contract(goal):
    """Describe coverage without changing a Goal or manufacturing a check result.

    A missing evaluator is distinct from an unknown target and from an unavailable
    execution dependency. Unknown requirements remain explicit obligations.
    """
    generic = goal.evidence_contract_version == "generic-observation-1"
    known_rules = {
        "orca-hf-1", "orca-hf-2", "evidence-read-1", "energy-compare-1",
        "finite-sampling-1", "finite-sampling-check-1", "knowledge-answer-1",
    }
    requirements = []
    for text in goal.minimum_evidence:
        canonical = LEGACY_NAMES.get(text, text)
        known = canonical in REQUIREMENTS or canonical in {goal.port, goal.minimum_check_version}
        requirements.append({"requested": text, "canonical": canonical,
                             "source": goal.minimum_evidence_source.get(text, "unknown"),
                             "evaluator": "registered" if known and (
                                 not generic or canonical == "purpose_preserved@1") else "unavailable"})
    return {"quantity": goal.quantity or goal.port, "port": goal.port,
            "contract_version": goal.evidence_contract_version,
            "verification": goal.verification,
            "scientific_checker": ("registered" if goal.minimum_check_version in known_rules
                                   and not generic else "unavailable"),
            "requirements": requirements}


def assess_minimum_evidence(goal, result, *, purpose_passed):
    """Retain each requested string and its mapping; unsupported never means pass."""
    entries = []
    for text in goal.minimum_evidence:
        canonical = LEGACY_NAMES.get(text, text)
        reason = None
        checks = result.checks.get(goal.port, [])
        if canonical == goal.port:
            passed = goal.port in result.qualified_outputs
        elif canonical == goal.minimum_check_version:
            passed = bool(checks) and all(c.status == "passed" and c.rule_version == canonical for c in checks)
        elif canonical in REQUIREMENTS:
            rule = REQUIREMENTS[canonical]
            if rule["ports"] is not None and goal.port not in rule["ports"]:
                passed, reason = False, "minimum_evidence_not_applicable_to_output"
            elif rule.get("predicate") == "current_use_passed":
                passed = purpose_passed
            elif "check" in rule:
                selected = [c for c in checks if c.name == rule["check"]]
                passed = bool(selected) and all(c.status == "passed" and c.rule_version == goal.minimum_check_version
                                                for c in selected)
            else:
                output = result.qualified_outputs.get(goal.port)
                rows = output.source.get("members", []) if output else []
                if not rows:
                    rows = next((data.get("members", []) for data in result.observations.values()
                                 if isinstance(data, dict) and data.get("members")), [])
                required = [row for row in rows if row.get("required")]
                passed = bool(required) and all(row.get("status") == "qualified" for row in required)
        else:
            passed, reason = False, "unsupported_minimum_evidence"
        entries.append({"requested": text, "canonical": canonical, "mapping_version": RULE_VERSION,
                        "status": "passed" if passed else "unresolved",
                        "reason": None if passed else reason or "minimum_evidence_missing_failed_or_wrong_version"})
    return entries
