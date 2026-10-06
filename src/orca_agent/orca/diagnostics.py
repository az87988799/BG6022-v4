"""Structured scientific diagnostics; these never grant execution permission."""

from __future__ import annotations

from typing import Any


def diagnostic(category: str, detail: str, source: dict[str, Any] | None = None) -> dict:
    return {"category": category, "detail": detail, "source": source or {}}


def scientific_diagnostics(observations: dict[str, Any], tool_name: str) -> list[dict]:
    facts = observations
    output = []
    if not facts.get("normal_termination"):
        output.append(diagnostic("abnormal_or_incomplete_output", "No final normal termination."))
    if not facts.get("scf_converged"):
        output.append(diagnostic("scf_not_converged", "Final SCF convergence is not established."))
    if tool_name == "orca.opt" and not facts.get("optimization_converged"):
        output.append(diagnostic(
            "optimization_not_converged", "No qualified optimized structure may be published."
        ))
    return output
