"""Bounded scientific analysis over explicitly qualified, immutable evidence.

These local records have no independent lifecycle. The caller obtains parameters
from the immutable Request and binds specific results before calling the pure
functions. No function chooses a next action, starts a calculation, or upgrades a
historical scientific check. XYZ parsing uses the existing production validator.
"""

from __future__ import annotations

import hashlib
import math
from collections import Counter
from typing import Annotated, Any, Literal

from pydantic import Field, model_validator

from orca_agent.models import CalculationParameters, Check, Identifier, Record
from orca_agent.orca.checks import check_outputs
from orca_agent.versions import CURRENT_CHECK_VERSION

Hash = Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]
Positive = Annotated[float, Field(gt=0)]
ENERGY_COMPARE_VERSION = "energy-compare-1"
SAMPLING_VERSION = "finite-sampling-1"
SAMPLING_CHECK_VERSION = "finite-sampling-check-1"


class EnergyConditions(Record):
    method: str | None = None
    basis: str | None = None
    charge: int | None = None
    multiplicity: int | None = None
    electronic_state: str | None = None
    environment: str | None = None


class EnergyEvidence(Record):
    run_id: Identifier
    attempt_id: Identifier
    result_id: Identifier
    tool: Literal["orca.sp", "orca.opt"]
    energy_eh: float
    unit: Literal["Eh"] = "Eh"
    artifact_hashes: Annotated[dict[Identifier, Hash], Field(min_length=1)]
    geometry_artifact_id: Identifier
    geometry_sha256: Hash
    elements: Annotated[list[str], Field(min_length=1, max_length=20)]
    atom_mapping: Annotated[list[str], Field(min_length=1, max_length=20)]
    coordinate_unit: Literal["angstrom"] = "angstrom"
    conditions: EnergyConditions
    checks: Annotated[list[Check], Field(min_length=1)]
    condition_evidence: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def qualified_source(self):
        if len(self.elements) != len(self.atom_mapping) or len(set(self.atom_mapping)) != len(
            self.atom_mapping
        ):
            raise ValueError("atom mapping must identify each ordered atom exactly once")
        if self.artifact_hashes.get(self.geometry_artifact_id) != self.geometry_sha256:
            raise ValueError("geometry is not bound to a verified source artifact")
        required = {c.name for c in check_outputs({}, "orca.sp")["energy"]}
        if not required.issubset({c.name for c in self.checks}):
            raise ValueError("source is missing required energy checks")
        if any(c.status != "passed" or c.rule_version != CURRENT_CHECK_VERSION for c in self.checks):
            raise ValueError("source energy requires passed orca-hf-2 checks; no automatic upgrade")
        return self


class AnalysisMember(Record):
    id: Identifier
    required: bool = True
    evidence: EnergyEvidence | None = None
    missing_reason: str | None = None
    unavailable_source: dict[str, Any] | None = None


class EnergyCompareParameters(Record):
    member_a: Identifier
    member_b: Identifier
    allow_different_geometries: bool = False
    quantity: str = "electronic_energy_difference"

    @model_validator(mode="after")
    def different_members(self):
        if self.member_a == self.member_b:
            raise ValueError("comparison requires two distinct member identities")
        return self


class SamplingParameters(Record):
    target_width_angstrom: Positive
    energy_threshold_eh: Positive
    fixed_bond_angstrom: Positive
    fixed_angle_degrees: Annotated[float, Field(gt=0, lt=180)]
    distance_tolerance_angstrom: Annotated[float, Field(gt=0, le=1e-6)] = 1e-8
    angle_tolerance_degrees: Annotated[float, Field(gt=0, le=1e-4)] = 1e-6
    coordinate_tolerance_angstrom: Annotated[float, Field(gt=0, le=1e-8)] = 1e-10
    units: Literal["angstrom"] = "angstrom"
    index_base: Literal[0] = 0
    elements: list[str] = Field(default_factory=lambda: ["O", "H", "H"])
    center_atom: Annotated[int, Field(strict=True, ge=0, le=2)] = 0
    scanned_atom: Annotated[int, Field(strict=True, ge=0, le=2)] = 1
    fixed_atom: Annotated[int, Field(strict=True, ge=0, le=2)] = 2
    # Optional coordinate anchor strengthens the one-dimensional profile; the
    # required fixed bond and angle checks always apply even without this anchor.
    fixed_coordinates: dict[int, tuple[float, float, float]] = Field(default_factory=dict)

    @model_validator(mode="after")
    def explicit_mapping(self):
        indices = (self.center_atom, self.scanned_atom, self.fixed_atom)
        if sorted(indices) != [0, 1, 2] or len(self.elements) != 3:
            raise ValueError("sampling requires three distinct mapped atom indices")
        if [self.elements[i] for i in indices] != ["O", "H", "H"]:
            raise ValueError("sampling requires an explicitly mapped O-H bond in H2O")
        if set(self.fixed_coordinates) - {self.center_atom, self.fixed_atom}:
            raise ValueError("coordinate anchor can only fix the center and fixed atom")
        return self


class SamplingCandidate(Record):
    id: Identifier
    artifact_id: Identifier
    sha256: Hash
    declared_r_angstrom: Positive
    required_initial: bool


def sampling_check_contract(parameters: SamplingParameters | None = None) -> dict:
    """Public predicate and effective tolerance; no proposed point or hidden energy."""
    tolerance = (parameters.distance_tolerance_angstrom if parameters is not None else
                 SamplingParameters.model_fields["distance_tolerance_angstrom"].default)
    return {
        "rule": "Current samples: interior min; each other sampled E-Emin>energy_threshold_eh; nearest left/right span<=target_width_angstrom+distance_tolerance_angstrom. New samples may change minimum and neighbors.",
        "distance_tolerance_angstrom" if parameters is not None else "default_distance_tolerance_angstrom": tolerance,
    }


def _coordinates(data: bytes):
    from orca_agent.tools.registry import validate_geometry

    if len(data) > 65536:
        raise ValueError("geometry exceeds the 64 KiB input limit")
    return validate_geometry(data.decode("utf-8"), CalculationParameters())


def bind_energy(store, run_id: str, result_id: str, *, expected_attempt_id=None) -> EnergyEvidence:
    """Verify a concrete persisted result and recheck every original source hash.

This reads no ORCA numeric data and adds no parsing chain. The production Result
has already passed the one parser/checker. Hash verification prevents its old
checks being reused for changed files. Missing provenance fails closed.
"""
    result = store.load_result(run_id, result_id)
    run = store.load_run(run_id)
    if result.id not in run.result_ids or result.operation_status != "completed":
        raise ValueError("source result is not a completed result bound to its Run")
    attempts = [a for a in run.attempts if a.id == result.attempt_id]
    if len(attempts) != 1 or attempts[0].step_id != result.step_id:
        raise ValueError("source result has no exact Attempt binding")
    attempt = attempts[0]
    if expected_attempt_id is not None and attempt.id != expected_attempt_id:
        raise ValueError("source attempt differs from the explicit consumption binding")
    if (result.source.get("input_fingerprint") != attempt.input_fingerprint
            or result.source.get("geometry_artifact_id") != attempt.geometry_artifact_id):
        raise ValueError("source input/geometry differs from its immutable Attempt binding")
    output = result.qualified_outputs.get("energy")
    if output is None or output.value is None or output.unit != "Eh":
        raise ValueError("source result has no qualified electronic energy in Eh")
    if output.checks != result.checks.get("energy"):
        raise ValueError("qualified output checks differ from the source Result")
    files = result.source.get("files", {})
    if not {"stdout.out", "job.inp", "geometry.xyz"}.issubset(files):
        raise ValueError("source lacks the original energy, input, or geometry evidence")
    hashes, paths = {}, {}
    for name, source in files.items():
        artifact = store.load_artifact(source["artifact_id"])
        if (artifact.id not in result.artifact_ids or artifact.sha256 != source["sha256"]
                or artifact.run_id != run_id or artifact.attempt_id != attempt.id):
            raise ValueError("source artifact hash or Run/Attempt provenance differs")
        hashes[artifact.id] = artifact.sha256
        paths[name] = store.artifact_path(artifact.id)
    if set(hashes) != set(result.artifact_ids):
        raise ValueError("source Result has an incomplete artifact manifest")
    geometry_name = "geometry.xyz"
    if attempt.tool == "orca.opt":
        from orca_agent.applicability import qualified_geometry
        qualified_geometry(store, result)
        geometry_name = "job.xyz"
    geometry_source = files[geometry_name]
    data = paths[geometry_name].read_bytes()
    if hashlib.sha256(data).hexdigest() != geometry_source["sha256"]:
        raise ValueError("geometry source changed during consumption")
    atoms = _coordinates(data)
    from orca_agent.applicability import source_conditions
    actual, condition_evidence = source_conditions(store, result)
    conditions = EnergyConditions(**actual)
    return EnergyEvidence(
        run_id=run_id, attempt_id=attempt.id, result_id=result.id, tool=attempt.tool,
        energy_eh=output.value, artifact_hashes=hashes,
        geometry_artifact_id=geometry_source["artifact_id"],
        geometry_sha256=geometry_source["sha256"], elements=[a[0] for a in atoms],
        atom_mapping=[f"{index}:{a[0]}" for index, a in enumerate(atoms)],
        conditions=conditions, checks=output.checks, condition_evidence=condition_evidence,
    )


def _members(members: list[AnalysisMember]) -> dict[str, AnalysisMember]:
    # Pydantic records are intentionally mutable locally. Revalidate at the
    # analysis boundary so assignment/model_copy cannot smuggle nonfinite values
    # or downgraded checks into a successful arithmetic comparison.
    for member in members:
        AnalysisMember.model_validate(member.model_dump())
    if not 1 <= len(members) <= 5 or len({m.id for m in members}) != len(members):
        raise ValueError("analysis requires one to five uniquely identified members")
    if not any(m.required for m in members):
        raise ValueError("analysis requires at least one required member")
    return {m.id: m for m in members}


def _rows(members: list[AnalysisMember]) -> list[dict[str, Any]]:
    return [{
        "member_id": m.id, "required": m.required,
        "status": "qualified" if m.evidence else "missing",
        "energy_eh": m.evidence.energy_eh if m.evidence else None,
        "missing_reason": None if m.evidence else m.missing_reason or "qualified_energy_missing",
        "source": m.evidence.model_dump(mode="json") if m.evidence else m.unavailable_source,
    } for m in members]


def _compatibility(first: EnergyEvidence, other: EnergyEvidence) -> str | None:
    if (Counter(first.elements) != {"H": 2, "O": 1}
            or first.elements != other.elements or first.atom_mapping != other.atom_mapping):
        return "incompatible_system_or_atom_mapping"
    if any(value is None for value in first.conditions.model_dump().values()) or any(
        value is None for value in other.conditions.model_dump().values()
    ):
        return "missing_required_conditions"
    if first.conditions != other.conditions:
        return "incompatible_scientific_conditions"
    if first.conditions != EnergyConditions(
        method="HF", basis="STO-3G", charge=0, multiplicity=1,
        electronic_state="RHF", environment="gas_phase",
    ):
        return "unsupported_scientific_conditions"
    return None


def _check(name: str, passed: bool, version: str, detail="") -> dict[str, Any]:
    return Check(name=name, status="passed" if passed else "failed",
                 rule_version=version, detail=detail).model_dump(mode="json")


def energy_compare(members: list[AnalysisMember], parameters: EnergyCompareParameters) -> dict:
    """Compute E(B)-E(A) only for the explicit compatible comparison relation."""
    parameters = EnergyCompareParameters.model_validate(parameters.model_dump())
    by_id = _members(members)
    result = {"operation_status": "completed", "rule_version": ENERGY_COMPARE_VERSION,
              "quantity": "electronic_energy_difference", "formula": "E(B) - E(A)",
              "unit": "Eh", "member_a": parameters.member_a, "member_b": parameters.member_b,
              "members": _rows(members), "qualified_outputs": {},
              "limitation": "Electronic energy at the specified geometries; not a free energy."}
    reason = None
    if parameters.quantity != "electronic_energy_difference":
        reason = "requested_quantity_not_supported"
    elif any(m.required and m.evidence is None for m in members):
        reason = "missing_required_member"
    elif any(key not in by_id or by_id[key].evidence is None
             for key in (parameters.member_a, parameters.member_b)):
        reason = "missing_comparison_member"
    else:
        first, second = (by_id[key].evidence for key in (parameters.member_a, parameters.member_b))
        reason = _compatibility(first, second)
        if not reason and first.geometry_sha256 != second.geometry_sha256 and not (
            parameters.allow_different_geometries
        ):
            reason = "geometry_difference_not_authorized"
        # A required additional member belongs to the same requested collection.
        for member in members:
            if not reason and member.required and member.evidence:
                reason = _compatibility(first, member.evidence)
    checks = [_check("compatible_complete_electronic_energy_comparison", reason is None,
                     ENERGY_COMPARE_VERSION, reason or "specified source energies are compatible")]
    result.update(scientific_status="passed" if reason is None else "insufficient_evidence",
                  reason=reason or "compatible_comparison", checks=checks)
    if reason is None:
        value = second.energy_eh - first.energy_eh
        if not math.isfinite(value):
            raise ValueError("derived electronic energy difference is not finite")
        result["qualified_outputs"]["energy_difference"] = {
            "value": value, "unit": "Eh", "checks": checks,
            "source": {"formula": "E(B) - E(A)", "A": first.model_dump(mode="json"),
                       "B": second.model_dump(mode="json")},
        }
    return result


def candidate_geometry(candidate: SamplingCandidate, data: bytes,
                       parameters: SamplingParameters) -> dict[str, float]:
    """Recompute the scan and fixed internal coordinates from verified XYZ bytes."""
    candidate = SamplingCandidate.model_validate(candidate.model_dump())
    parameters = SamplingParameters.model_validate(parameters.model_dump())
    if hashlib.sha256(data).hexdigest() != candidate.sha256:
        raise ValueError("candidate geometry hash mismatch")
    atoms = _coordinates(data)
    if [a[0] for a in atoms] != parameters.elements:
        raise ValueError("candidate geometry atom mapping mismatch")
    points = [a[1:] for a in atoms]
    center, scanned, fixed = [points[i] for i in (
        parameters.center_atom, parameters.scanned_atom, parameters.fixed_atom,
    )]
    r, fixed_r = math.dist(center, scanned), math.dist(center, fixed)
    cosine = sum((a - o) * (b - o) for o, a, b in zip(center, scanned, fixed, strict=True))
    angle = math.degrees(math.acos(max(-1.0, min(1.0, cosine / (r * fixed_r)))))
    if abs(r - candidate.declared_r_angstrom) > parameters.distance_tolerance_angstrom:
        raise ValueError("declared scan distance differs from actual XYZ")
    if abs(fixed_r - parameters.fixed_bond_angstrom) > parameters.distance_tolerance_angstrom:
        raise ValueError("fixed bond differs from the immutable sampling goal")
    if abs(angle - parameters.fixed_angle_degrees) > parameters.angle_tolerance_degrees:
        raise ValueError("fixed angle differs from the immutable sampling goal")
    if any(math.dist(points[index], reference) > parameters.coordinate_tolerance_angstrom
           for index, reference in parameters.fixed_coordinates.items()):
        raise ValueError("fixed atom coordinates changed")
    return {"r_angstrom": r, "fixed_bond_angstrom": fixed_r, "angle_degrees": angle}


def finite_sampling(candidates: list[SamplingCandidate], members: list[AnalysisMember],
                    geometry_bytes: dict[str, bytes], parameters: SamplingParameters) -> dict:
    """Judge a finite discrete sampling goal without proposing any next action."""
    parameters = SamplingParameters.model_validate(parameters.model_dump())
    candidates = [SamplingCandidate.model_validate(c.model_dump()) for c in candidates]
    by_id = _members(members)
    if len(candidates) != 5 or len({c.id for c in candidates}) != 5:
        raise ValueError("sampling requires five uniquely registered candidates")
    if len({c.artifact_id for c in candidates}) != 5:
        raise ValueError("sampling candidates require distinct geometry artifacts")
    if sum(c.required_initial for c in candidates) != 3:
        raise ValueError("sampling requires exactly three initial required candidates")
    candidate_map = {c.id: c for c in candidates}
    if set(by_id) != set(candidate_map):
        raise ValueError("sampling members must describe all registered candidates")
    if any(by_id[c.id].required != c.required_initial for c in candidates):
        raise ValueError("required sampling membership differs from the immutable goal")
    facts, invalid = {}, {}
    for candidate in candidates:
        try:
            facts[candidate.id] = candidate_geometry(
                candidate, geometry_bytes[candidate.artifact_id], parameters,
            )
        except (KeyError, ValueError, UnicodeError) as exc:
            invalid[candidate.id] = str(exc) if not isinstance(exc, KeyError) else "geometry_missing"
    result = {"operation_status": "completed", "rule_version": SAMPLING_VERSION,
              "acceptance_criteria": sampling_check_contract(parameters),
              "members": _rows(members), "geometry_facts": facts, "invalid_candidates": invalid,
              "target_width_angstrom": parameters.target_width_angstrom,
              "energy_threshold_eh": parameters.energy_threshold_eh,
              "qualified_outputs": {}, "goal_satisfied": False,
              "limitation": "Only the sampled discrete range; no continuous or global minimum, "
                            "vibrational stability, transition state, or full potential energy surface."}
    reason = "invalid_candidate_geometry" if invalid else None
    ordered = sorted(facts, key=lambda key: facts[key]["r_angstrom"])
    if not reason:
        distances = [facts[key]["r_angstrom"] for key in ordered]
        if any(b - a <= parameters.distance_tolerance_angstrom
               for a, b in zip(distances, distances[1:])):
            reason = "candidate_distances_not_distinct"
        elif {ordered[i] for i in (0, 2, 4)} != {
            c.id for c in candidates if c.required_initial
        }:
            reason = "initial_members_not_coarse_points"
        elif any(abs(r - (distances[0] + i * (distances[-1] - distances[0]) / 4))
                 > parameters.distance_tolerance_angstrom for i, r in enumerate(distances)):
            reason = "candidates_not_registered_half_step_grid"
    sampled = [key for key in ordered if by_id[key].evidence is not None]
    result["sampled_candidate_ids"] = sampled
    result["unsampled_candidate_ids"] = [key for key in ordered if key not in sampled]
    if not reason and any(m.required and m.evidence is None for m in members):
        reason = "missing_required_energy"
    if not reason:
        first = by_id[sampled[0]].evidence
        for key in sampled:
            energy, candidate = by_id[key].evidence, candidate_map[key]
            reason = _compatibility(first, energy)
            if reason:
                break
            if (energy.tool != "orca.sp" or energy.geometry_sha256 != candidate.sha256
                    or energy.elements != parameters.elements):
                reason = "energy_geometry_binding_mismatch"
                break
    if not reason:
        minimum = min(sampled, key=lambda key: by_id[key].evidence.energy_eh)
        position = sampled.index(minimum)
        energy_min = by_id[minimum].evidence.energy_eh
        result["observed_lowest_candidate_id"] = minimum
        if position in (0, len(sampled) - 1):
            reason = "boundary_minimum"
        elif any(by_id[key].evidence.energy_eh - energy_min <= parameters.energy_threshold_eh
                 for key in sampled if key != minimum):
            reason = "not_numerically_distinct"
        else:
            left, right = sampled[position - 1], sampled[position + 1]
            span = facts[right]["r_angstrom"] - facts[left]["r_angstrom"]
            result.update(minimum_candidate_id=minimum, left_neighbor_id=left,
                          right_neighbor_id=right, neighbor_span_angstrom=span,
                          left_gap_eh=by_id[left].evidence.energy_eh - energy_min,
                          right_gap_eh=by_id[right].evidence.energy_eh - energy_min)
            if span > parameters.target_width_angstrom + parameters.distance_tolerance_angstrom:
                reason = "span_too_wide"
    result.update(goal_satisfied=reason is None,
                  scientific_status="passed" if reason is None else "insufficient_evidence",
                  reason=reason or "sufficient_discrete_evidence")
    checks = [_check("finite_discrete_sampling", reason is None, SAMPLING_VERSION, result["reason"])]
    result["checks"] = checks
    if reason is None:
        result["qualified_outputs"]["sampling"] = {
            "value": result["neighbor_span_angstrom"], "unit": "angstrom", "checks": checks,
            "source": {"minimum_candidate_id": result["minimum_candidate_id"],
                       "sampled": [by_id[key].evidence.model_dump(mode="json") for key in sampled],
                       "geometry_facts": facts, "goal": parameters.model_dump(mode="json")},
        }
    return result


def sampling_check(candidates: list[SamplingCandidate], members: list[AnalysisMember],
                   geometry_bytes: dict[str, bytes], parameters: SamplingParameters) -> dict:
    """Check the same finite predicate without claiming a qualified sampling goal.

    A determinate negative is a completed checking task. Missing inputs or
    invalid provenance/conditions cannot be promoted into a checked answer.
    """
    result = finite_sampling(candidates, members, geometry_bytes, parameters)
    determinate = result["reason"] in {
        "sufficient_discrete_evidence", "span_too_wide", "boundary_minimum", "not_numerically_distinct"}
    predicate_checks = result["checks"]
    checks = [_check("finite_sampling_predicate_determined", determinate, SAMPLING_CHECK_VERSION,
                     result["reason"])]
    result.update(rule_version=SAMPLING_CHECK_VERSION, predicate_rule_version=SAMPLING_VERSION,
                  predicate_checks=predicate_checks, checks=checks, qualified_outputs={},
                  assessment_status="determinate" if determinate else "insufficient_evidence",
                  predicate_satisfied=result["goal_satisfied"] if determinate else None)
    result["checked_artifact_outputs"] = ({"sampling_check": {"checks": checks, "source": {
        "predicate_satisfied": result["predicate_satisfied"], "predicate_rule_version": SAMPLING_VERSION,
        "reason": result["reason"], "limitation": result["limitation"]}}} if determinate else {})
    return result
