"""Deterministic checks for the single admitted RHF/STO-3G profile."""

from __future__ import annotations

import math
from typing import Any

from orca_agent.models import Check
from orca_agent.versions import CURRENT_CHECK_VERSION

OPTIMIZATION_STAGE_RULE = "optimization-final-stage-1"


def check_outputs(observations: dict[str, Any], tool_name: str) -> dict[str, list[Check]]:
    facts = observations

    def check(name: str, passed: bool | None, detail: str = "") -> Check:
        status = "unverified" if passed is None else ("passed" if passed else "failed")
        return Check(
            name=name, status=status, detail=detail,
            rule_version=CURRENT_CHECK_VERSION,
            source=facts.get("evidence", {}).get(name, {}),
        )

    energy = facts.get("energy_eh")
    common = [
        check("input_integrity", facts.get("input_integrity")),
        check("normal_termination", facts.get("normal_termination")),
        check("orca_version", facts.get("version_supported")),
        check("method_and_electronic_state", facts.get("conditions_match")),
        check("initial_geometry", facts.get("initial_geometry_matches")),
        check("parser_consistency", facts.get("parser_consistent")),
    ]
    energy_checks = common + [
        check("scf_converged", facts.get("scf_converged")),
        check("finite_total_energy", isinstance(energy, (int, float)) and math.isfinite(energy)),
        check("energy_geometry_binding", facts.get("energy_geometry_bound")),
    ]
    result = {"energy": energy_checks}
    if tool_name == "orca.opt":
        result["optimized_geometry"] = energy_checks + [
            check("optimization_converged", facts.get("optimization_converged")),
            check("optimization_thresholds", facts.get("optimization_thresholds_passed")),
            check("optimization_stage_binding", facts.get("optimization_stage_bound"),
                  "; ".join(facts.get("optimization_stage", {}).get("reasons", []))),
            check("final_geometry", facts.get("final_geometry_matches")),
        ]
    result["dipole_moment"] = list(result.get("optimized_geometry", energy_checks)) + [
        check("dipole_binding", facts.get("dipole_bound"),
              facts.get("dipole_error", "Unique OPI/text property, units and geometry frame agree.")),
    ]
    return result
