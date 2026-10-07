"""Read-only independent grading of persisted joint trajectories.

Expected numbers/actions come only from the frozen B-01 independent review and raw references. This
helper never imports the production parser, analysis tools or goal judgment,
and never sends a model request, constructs a Plan or launches a process.
"""

from __future__ import annotations

import argparse
import json
import math
import re
from decimal import Decimal
from pathlib import Path, PureWindowsPath

from orca_agent.models import fingerprint
from orca_agent.store import sha256_file
from tests.helpers.phase_b_reference import independent_output
from tests.helpers.phase_b_review import geometry_facts, sampling_judgment, verify_candidates

PROJECT = Path(__file__).resolve().parents[2]
FROZEN = {
    "docs/acceptance/phase-b/reference-review.json": "0ac7e0c1da1021862464686b25fe9bda3ae5e0a5227ddcb0eec0eccc10c19f10",
    "tests/fixtures/phase_b/cases.json": "ed3ed06a5027468388cdced66a9ce321889897c28289d8a7e02dd95050af8b30",
    "tests/fixtures/phase_b/sampling-candidates.json": "c542b8d001e577686d709f792e2504beea3d9331348a06f05774b8009befaef4",
}
CASES = ("water_sp", "repair_success", "repair_exhaustion", "sampling_left", "sampling_right",
         "sampling_stop", "methane_opt_control")
ENERGY_CHECKS = {"input_integrity", "normal_termination", "orca_version", "method_and_electronic_state",
                 "initial_geometry", "parser_consistency", "scf_converged", "finite_total_energy",
                 "energy_geometry_binding"}
GEOMETRY_CHECKS = ENERGY_CHECKS | {"optimization_converged", "optimization_thresholds", "final_geometry"}


def _json(path, *, limit=16 * 1024 * 1024):
    if path.stat().st_size > limit:
        raise ValueError("grading evidence exceeds its bounded size")
    return json.loads(path.read_text(encoding="utf-8"))


def _verified(entry):
    name = entry["path"]
    if Path(name).is_absolute() or PureWindowsPath(name).is_absolute():
        mapping = _json(PROJECT / "tests/fixtures/phase_b/independent/reference-copies.json")
        matches = [item for item in mapping["copies"]
                   if item["historical_path"] == name and item["sha256"] == entry["sha256"]]
        if len(matches) != 1:
            raise ValueError("historical reference has no unique controlled copy")
        name = matches[0]["repository_path"]
    path = (PROJECT / name).resolve()
    if not path.is_relative_to(PROJECT.resolve()):
        raise ValueError("frozen reference path is outside the repository")
    if sha256_file(path) != entry["sha256"]:
        raise ValueError("frozen reference hash changed")
    return path


def frozen_references():
    values = {name: _json(_verified({"path": name, "sha256": digest})) for name, digest in FROZEN.items()}
    return (values["docs/acceptance/phase-b/reference-review.json"],
            values["tests/fixtures/phase_b/cases.json"],
            values["tests/fixtures/phase_b/sampling-candidates.json"])


def _qualified(result, port, names, version="orca-hf-2"):
    output = result.qualified_outputs.get(port)
    return bool(result.operation_status == "completed" and output
                and output.checks == result.checks.get(port)
                and {c.name for c in output.checks} == names
                and all(c.status == "passed" and c.rule_version == version for c in output.checks))


def _attempt_evidence(store, run, attempt):
    """Verify concrete provenance first; numerical reading is independent."""
    if not attempt.result_id or attempt.result_id not in run.result_ids:
        raise ValueError("Attempt has no Run-bound Result")
    result = store.load_result(run.id, attempt.result_id)
    if (result.run_id, result.attempt_id, result.step_id) != (run.id, attempt.id, attempt.step_id):
        raise ValueError("Attempt/Result identity mismatch")
    geometry = store.load_artifact(attempt.geometry_artifact_id)
    store.artifact_path(geometry.id)
    params = attempt.frozen_step.parameters.model_dump() if attempt.frozen_step else None
    if not params or attempt.input_fingerprint != fingerprint({
            "tool": attempt.tool, "parameters": params, "geometry_hash": geometry.sha256}):
        raise ValueError("frozen input fingerprint mismatch")
    if (result.source.get("input_fingerprint") != attempt.input_fingerprint
            or result.source.get("geometry_artifact_id") != geometry.id):
        raise ValueError("Result input provenance mismatch")
    files, hashes = {}, {}
    for name, entry in result.source.get("files", {}).items():
        artifact = store.load_artifact(entry["artifact_id"])
        if (artifact.id not in result.artifact_ids or artifact.sha256 != entry["sha256"]
                or (artifact.run_id, artifact.attempt_id) != (run.id, attempt.id)):
            raise ValueError("Result raw source provenance mismatch")
        files[name] = store.artifact_path(artifact.id)
        hashes[artifact.id] = artifact.sha256
    if set(hashes) != set(result.artifact_ids) or not {"stdout.out", "job.inp", "geometry.xyz", "execution.json"} <= set(files):
        raise ValueError("required raw source or complete manifest missing")
    if sha256_file(files["geometry.xyz"]) != geometry.sha256:
        raise ValueError("actual initial geometry differs")
    execution = _json(files["execution.json"])
    raw = independent_output(files["stdout.out"])
    return {"attempt": attempt, "result": result, "parameters": params, "files": files,
            "geometry_sha256": geometry.sha256, "raw": raw, "execution": execution,
            "artifact_hashes": hashes}


def _input_profile(evidence, tool):
    text = evidence["files"]["job.inp"].read_text(encoding="utf-8").upper()
    parameters = evidence["parameters"]
    compact = re.sub(r"\s+", " ", text)
    scf = re.search(r"%SCF\b(.*?)\bEND\b", compact)
    required = ("RHF", "STO-3G", "TIGHTSCF", "NORI", "NOAUTOSTART", "CONVFORCED 1",
                "NPROCS 4", "%MAXCORE 192", "XYZFILE 0 1 GEOMETRY.XYZ")
    return (all(item in compact for item in required)
            and scf is not None and re.search(r"\bMAXITER\s+" + str(parameters["scf_maxiter"]) + r"\b", scf[1]) is not None
            and ("TIGHTOPT" in text) == (tool == "orca.opt")
            and (tool != "orca.opt" or "ENFORCESTRICTCONVERGENCE TRUE" in compact)
            and all(parameters.get(key) == value for key, value in {
                "method": "HF", "basis": "STO-3G", "charge": 0, "multiplicity": 1,
                "cores": 4, "maxcore_mb": 192, "memory_mb": 1024}.items()))


def _resources(evidence):
    execution = evidence["execution"]
    resource = execution.get("resource_usage", {})
    handle = execution.get("handle", {})
    return bool(handle.get("pid") and handle.get("create_time") and handle.get("atomic_job_assignment")
                and resource.get("active_processes") == 0 and resource.get("cores") == 4
                and resource.get("job_commit_limit_bytes") == 1024 * 1024 * 1024
                and 0 < resource.get("peak_job_commit_bytes", 0) <= 1024 * 1024 * 1024
                and execution.get("postprocess_starts_detected") == 0)


def _strict_opt(path):
    text = path.read_bytes().decode("utf-8")
    start = text.rfind("Geometry convergence")
    table = text[start:] if start >= 0 else ""
    limits = {"Energy change": 1e-6, "RMS gradient": 3e-5, "MAX gradient": 1e-4,
              "RMS step": 6e-4, "MAX step": 1e-3}
    rows = {}
    for name, limit in limits.items():
        match = re.search(re.escape(name) + r"\s+([-+0-9.eE]+)\s+([-+0-9.eE]+)\s+(YES|NO)", table)
        if not match:
            return False
        value, tolerance = float(match[1]), float(match[2])
        rows[name] = (math.isfinite(value) and 0 < tolerance <= limit
                      and abs(value) <= tolerance and match[3] == "YES")
    return all(rows.values()) and "THE OPTIMIZATION HAS CONVERGED" in table


def _distances(path):
    rows = path.read_text(encoding="utf-8").splitlines()
    atoms = [line.split() for line in rows[2:] if line.strip()]
    if len(atoms) != int(rows[0]) or len(atoms) != 5 or [a[0] for a in atoms] != ["C", "H", "H", "H", "H"]:
        raise ValueError("methane atom identity/order differs")
    points = [[float(v) for v in atom[1:]] for atom in atoms]
    if any(len(p) != 3 or not all(math.isfinite(v) for v in p) for p in points):
        raise ValueError("nonfinite final geometry")
    return [math.dist(points[i], points[j]) for i in range(5) for j in range(i)]


def _model_evidence(store, run):
    valid = {}
    for record in run.model_records:
        request = store.path(f"runs/{run.id}/model/{record['id']}.request.json")
        response = store.path(f"runs/{run.id}/model/{record['id']}.response.json")
        if (sha256_file(request) != record["request_hash"]
                or sha256_file(response) != record.get("response_record_sha256")):
            raise ValueError("model receipt hash differs")
        data = _json(response, limit=128 * 1024)
        if (record.get("status") == "known" and record.get("http_status") == 200
                and not record.get("error_category") and data.get("proposal")):
            valid[record["id"]] = data["proposal"]
    initial, plans = False, []
    for decision in run.decisions:
        if "record_sha256" in decision:
            saved = _json(store.path(f"runs/{run.id}/decisions/{decision['id']}.json"))
            if fingerprint(saved) != decision["record_sha256"]:
                raise ValueError("accepted Plan decision receipt changed")
            if (valid.get(decision["id"], {}).get("action") in {"initial_plan", "revise_plan"}
                    and saved.get("plan") and saved["id"] == decision["id"]
                    and saved["plan"]["id"] == decision["plan_id"]
                    and saved["basis"] == decision["basis"]):
                plans.append(saved)
        if valid.get(decision["id"], {}).get("action") != "initial_plan":
            continue
        if decision.get("action") == "initial_plan":
            initial = True
        elif "record_sha256" in decision and decision.get("prior_plan_id") is None:
            path = store.path(f"runs/{run.id}/decisions/{decision['id']}.json")
            saved = _json(path)
            if fingerprint(saved) != decision["record_sha256"]:
                raise ValueError("accepted Plan decision receipt changed")
            initial = (saved["id"] == decision["id"] and saved["plan"]["id"] == decision["plan_id"]
                       and saved["basis"] == decision["basis"])
    stops = [d for d in run.decisions if d["id"] in valid
             and d.get("action") == valid[d["id"]].get("action") == "stop"
             and isinstance(d.get("reason"), str) and d["reason"].strip()
             and d["reason"] == valid[d["id"]].get("reason")]
    return initial, stops, plans


def grade_joint(store, run_or_id, case, *, metadata=None):
    """Return independent pass/fail evidence; never writes or trusts goal_status."""
    if case not in CASES:
        raise ValueError("unknown frozen joint case")
    run = store.load_run(run_or_id if isinstance(run_or_id, str) else run_or_id.id)
    result = {"schema_version": 1, "case": case, "run_id": run.id, "passed": False,
              "evidence_type": "independent_grading_of_persisted_joint_evidence",
              "uses_product_scientific_parser": False, "uses_goal_status_as_science_oracle": False,
              "frozen_sources": FROZEN, "checks": [], "attempts": [],
              "recorded_goal_status": run.goal_status,
              "limitation": "Same ORCA engine references; no cross-engine, global-minimum or vibrational guarantee."}
    # Failed planning still consumed real HTTP and tokens. Preserve that account
    # even when there is no Plan, Attempt, selected Result or scientific output.
    token_records = [r for r in run.model_records if r.get("total_tokens") is not None]
    cost_records = [r for r in run.model_records if r.get("cost_known_usd") is not None]
    result["recorded_cost"] = {
        "model_http_records": len(run.model_records),
        "known_token_records": len(token_records),
        "known_tokens": sum(r["total_tokens"] for r in token_records) if token_records else None,
        "unknown_usage_records": sum(r.get("status") != "known" for r in run.model_records),
        "cost_upper_usd": str(sum((Decimal(str(r["cost_known_usd"])) for r in cost_records),
                                   Decimal("0"))) if cost_records else None,
        "cost_basis": "known tokens at frozen maximum uncached price; provider charge not queried",
        "orca_starts_reserved": run.usage.orca_starts_reserved,
        "orca_starts_actual": run.usage.orca_starts_actual,
    }

    def check(name, passed, actual=None, expected=None):
        category = ("integrity" if name in {"frozen_reference_hashes", "evaluation_identity", "evidence_available_and_unchanged"}
                    else "protocol" if name in {"real_model_initial_plan_receipt", "accepted_final_explanation", "terminal_delivery",
                                                 "raw_intake_and_optimized_energy_target"}
                    else "trajectory" if name in {"exact_attempt_and_launch_count", "no_postprocess_or_unresolved_execution",
                        "one_repair_same_logical_step", "repair_changes_only_scf_maxiter", "initial_three_then_correct_half_or_stop",
                        "initial_sampling_plan_exactly_three_then_analysis", "sampling_append_follows_bound_analysis_feedback"}
                    else "scientific")
        result["checks"].append({"name": name, "category": category, "passed": bool(passed), "actual": actual, "expected": expected})

    try:
        review, cases, manifest = frozen_references()
        check("frozen_reference_hashes", True)
        if metadata is not None:
            check("evaluation_identity", metadata.get("run_id") == run.id and metadata.get("case") == case
                  and metadata.get("category") == run.batch_category)
        expected_count = 3 if case == "sampling_stop" else 4 if case.startswith("sampling_") else 2 if case.startswith("repair_") else 1
        check("exact_attempt_and_launch_count", len(run.attempts) == expected_count
              and sum(a.started for a in run.attempts) == expected_count
              and run.usage.orca_starts_reserved == run.usage.orca_starts_actual == expected_count,
              {"attempts": len(run.attempts), "reserved": run.usage.orca_starts_reserved,
               "actual": run.usage.orca_starts_actual}, expected_count)
        check("no_postprocess_or_unresolved_execution", run.usage.postprocess_starts == 0
              and all(a.finished_at is not None and a.state not in {"intent", "running", "unknown"} for a in run.attempts))
        initial, stops, accepted_plans = _model_evidence(store, run)
        check("real_model_initial_plan_receipt", initial)
        if metadata and metadata.get("input_form") == "raw_text":
            path = Path(metadata["raw_bundle_path"]).resolve()
            if not path.is_relative_to(store.root.resolve()) or sha256_file(path) != metadata["raw_bundle_sha256"]:
                raise ValueError("raw joint input bundle identity changed")
            raw_bundle = _json(path)
            request = store.load_request(run)
            normalized = [d for d in run.decisions if d.get("semantics", {}).get("kind") == "normalize"]
            records = {r["id"]: r for r in run.model_records}
            bound = bool(normalized and all(d["id"] in records for d in normalized))
            for decision in normalized:
                response = _json(store.path(f"runs/{run.id}/model/{decision['id']}.response.json"))
                bound &= response.get("proposal", {}).get("action") == "normalize_request"
            ports = {goal.port for goal in request.goals if goal.required}
            check("raw_intake_and_optimized_energy_target", bound and "goals" not in raw_bundle
                  and not {"method", "basis", "charge", "multiplicity", "environment", "electronic_state"}
                      & set(raw_bundle.get("conditions", {}))
                  and request.original_text == raw_bundle["text"]
                  and {"energy", "optimized_geometry"} <= ports
                  and all(goal.conditions.get("geometry_relation") == "optimized"
                          for goal in request.goals if goal.required and goal.port == "energy"))
        check("accepted_final_explanation", bool(stops))
        result["explanation"] = {"status": "pending_independent_rubric_review" if stops else "missing_accepted_final_explanation",
                                 "decision_id": stops[-1]["id"] if stops else None,
                                 "rubric_axes": cases["common"]["explanation_rubric"],
                                 "quality_passed": None}
        evidence = [_attempt_evidence(store, run, a) for a in run.attempts]
        for item in evidence:
            attempt, source = item["attempt"], item["result"]
            result["attempts"].append({"attempt_id": attempt.id, "result_id": source.id,
                                       "geometry_sha256": item["geometry_sha256"],
                                       "scf_maxiter": item["parameters"]["scf_maxiter"],
                                       "raw_observation": item["raw"], "artifact_hashes": item["artifact_hashes"]})
            tool = "orca.opt" if case == "methane_opt_control" else "orca.sp"
            check(f"{attempt.id}:frozen_profile", attempt.tool == tool and _input_profile(item, tool))
            check(f"{attempt.id}:execution_resources", _resources(item))
            if item["raw"]["status"] == "converged":
                output = source.qualified_outputs.get("energy")
                check(f"{attempt.id}:qualified_raw_energy", _qualified(source, "energy", ENERGY_CHECKS)
                      and output.unit == "Eh" and output.value is not None
                      and abs(output.value - item["raw"]["energy_eh"]) <= item["raw"]["print_rounding_eh"])
            else:
                check(f"{attempt.id}:failed_energy_withheld", "energy" not in source.qualified_outputs)
        if case.startswith("sampling_"):
            _grade_sampling(store, run, case, evidence, review, manifest, check, result)
            _grade_sampling_decisions(store, run, evidence, accepted_plans, check)
        else:
            _grade_single(case, evidence, review, cases, check)
        check("terminal_delivery", run.state in ({"failed", "budget_exhausted"} if case == "repair_exhaustion" else {"completed"}), run.state)
    except (ValueError, KeyError, TypeError, OSError, RuntimeError) as exc:
        check("evidence_available_and_unchanged", False, type(exc).__name__)
    result["passed"] = bool(result["checks"] and all(item["passed"] for item in result["checks"]))
    for category, label in (("scientific", "scientific_evidence_passed"), ("trajectory", "trajectory_passed"),
                            ("protocol", "protocol_passed")):
        selected = [c for c in result["checks"] if c["category"] in {category, "integrity"}]
        result[label] = any(c["category"] == category for c in selected) and all(c["passed"] for c in selected)
    result["scientific_success"] = result["scientific_evidence_passed"] and case != "repair_exhaustion"
    result["acceptance_scope"] = "Mechanical trajectory/evidence predicates only; explanation quality requires the separate six-axis review."
    return result


def _grade_single(case, evidence, review, cases, check):
    check("scientific_attempt_evidence_available", bool(evidence))
    methane = case == "methane_opt_control"
    name = "methane_opt" if methane else "water_sp"
    fixed = cases["common"]["fixed_input_files"][name]
    _verified(fixed)
    check("original_geometry_preserved", all(e["geometry_sha256"] == fixed["sha256"] for e in evidence))
    if case.startswith("repair_"):
        second = 100 if case == "repair_success" else 2
        check("one_repair_same_logical_step", len(evidence) == 2
              and len({e["attempt"].logical_id for e in evidence}) == 1
              and [e["attempt"].number for e in evidence] == [1, 2]
              and [e["parameters"]["scf_maxiter"] for e in evidence] == [1, second])
        if len(evidence) != 2:
            return
        changed = {key for key, value in evidence[0]["parameters"].items() if evidence[1]["parameters"].get(key) != value}
        check("repair_changes_only_scf_maxiter", changed == {"scf_maxiter"}, sorted(changed))
        failure = independent_output(_verified(review["source_index"]["phase-a/water_scf_limit"]["raw_failure"]))
        check("first_true_scf_failure", failure["status"] == evidence[0]["raw"]["status"] == "scf_not_converged"
              and len(evidence[0]["raw"].get("iterations", [])) == 1)
        if case == "repair_exhaustion":
            second_ref = review["source_index"]["phase-b/scf-maxiter-2"]
            observed = independent_output(_verified(second_ref["raw_stdout"]))
            check("second_true_scf_failure_no_third_start", observed["status"] == evidence[1]["raw"]["status"] == "scf_not_converged"
                  and len(evidence[1]["raw"].get("iterations", [])) == 2
                  and all(not e["result"].qualified_outputs for e in evidence))
            return
    if not evidence:
        return
    reference = review["source_index"][f"phase-a/{name}"]
    # This independently read number was sealed by B-01; its immutable review
    # remains portable when the complete historical archive is not mounted.
    expected_energy = reference["energy"]["reference_eh"]
    raw_entry = next(e for e in reference["files"] if e["path"].endswith("stdout.out"))
    observed = independent_output(_verified(raw_entry))
    if observed["status"] != "converged" or observed["energy_eh"] != expected_energy:
        raise ValueError("independent reference review differs from its raw evidence")
    final = evidence[-1]
    check("energy_matches_independent_reference", final["raw"]["status"] == "converged"
          and abs(final["raw"]["energy_eh"] - expected_energy) <= reference["energy"]["tolerance_eh"],
          final["raw"].get("energy_eh"), expected_energy)
    if methane:
        reference_xyz = next(e for e in reference["files"] if e["path"].endswith("job.xyz"))
        expected_distances = _distances(_verified(reference_xyz))
        final_distances = _distances(final["files"]["job.xyz"])
        output = final["result"].qualified_outputs.get("optimized_geometry")
        expected_id = final["result"].source["files"]["job.xyz"]["artifact_id"]
        check("strict_optimization_and_bound_final_geometry", _strict_opt(final["files"]["stdout.out"])
              and _qualified(final["result"], "optimized_geometry", GEOMETRY_CHECKS)
              and output.artifact_id == expected_id)
        difference = max(abs(a - b) for a, b in zip(expected_distances, final_distances, strict=True))
        check("geometry_matches_independent_reference", difference <= reference["geometry"]["tolerance_angstrom"], difference)


def _grade_sampling(store, run, case, evidence, review, manifest, check, report):
    window = next(w for w in manifest["windows"] if w["id"] == case.removeprefix("sampling_"))
    expected = next(w for w in review["sampling"]["windows"] if w["window_id"] == window["id"])
    facts = verify_candidates(PROJECT, manifest)
    by_hash = {c["sha256"]: c for c in window["candidates"]}
    sampled, energies = [], {}
    for item in evidence:
        candidate = by_hash[item["geometry_sha256"]]
        sampled.append(candidate["id"])
        raw_reference = review["sampling"]["raw_records"][candidate["id"]]
        original = independent_output(_verified(raw_reference["raw_stdout"]))
        actual = item["raw"]
        check(f"{candidate['id']}:reference_energy", actual["status"] == original["status"] == "converged"
              and abs(actual["energy_eh"] - original["energy_eh"]) <= 1e-7)
        if actual["status"] == "converged":
            energies[candidate["id"]] = actual["energy_eh"]
        actual_facts = geometry_facts(item["files"]["geometry.xyz"])
        check(f"{candidate['id']}:actual_xyz", actual_facts == facts[candidate["id"]])
    expected_sampled = window["initial_candidate_ids"] + ([expected["selected_candidate_id"]] if expected["selected_candidate_id"] else [])
    check("initial_three_then_correct_half_or_stop", len(sampled) == len(set(sampled))
          and set(sampled[:3]) == set(window["initial_candidate_ids"])
          and sampled[3:] == expected_sampled[3:], sampled, expected_sampled)
    independent = sampling_judgment(sampled, facts, energies, window["target_width_angstrom"],
                                    review["sampling"]["threshold_eh"], manifest["geometry_tolerances"]["distance_angstrom"])
    report["sampling_independent_judgment"] = independent
    check("independent_discrete_goal", independent["satisfied"] and independent.get("minimum_candidate_id") == expected["after_action"]["minimum_candidate_id"])
    plan = store.load_plan(run)
    binding = plan.goal_map.get("sampling_goal") if plan else None
    selected = run.selected_results.get(binding.step_id) if binding else None
    available = bool(binding and binding.port == "sampling" and selected in run.result_ids)
    check("sampling_analysis_result_available", available)
    if not available:
        check("qualified_sampling_bound_to_actual_results", False)
        check("immutable_sampling_analysis_artifact", False)
        check("complete_five_member_table", False)
        return
    derived = store.load_result(run.id, selected)
    output = derived.qualified_outputs.get("sampling")
    aliases = {c["id"]: c["model_candidate_id"] for c in window["candidates"]}
    sampled_ids = {e["result"].id for e in evidence}
    actual_sources = {e["result"].id: e for e in evidence}
    published_sources = output.source.get("sampled", []) if output else []
    exact_sources = all(s.get("result_id") in actual_sources
        and s.get("run_id") == run.id and s.get("attempt_id") == actual_sources[s["result_id"]]["attempt"].id
        and s.get("artifact_hashes") == actual_sources[s["result_id"]]["artifact_hashes"]
        and s.get("energy_eh") == actual_sources[s["result_id"]]["result"].qualified_outputs["energy"].value
        for s in published_sources)
    check("qualified_sampling_bound_to_actual_results", _qualified(derived, "sampling", {"finite_discrete_sampling"}, "finite-sampling-1")
          and output.unit == "angstrom" and output.value is not None
          and abs(output.value - independent.get("neighbor_span_angstrom", math.inf)) <= 1e-8
          and output.source.get("minimum_candidate_id") == aliases.get(independent.get("minimum_candidate_id"))
          and len(published_sources) == len(sampled_ids) and exact_sources
          and {e["result_id"] for e in published_sources} == sampled_ids
          and derived.id in run.result_ids and derived.step_id == binding.step_id and binding.port == "sampling")
    archives = []
    for artifact_id in derived.artifact_ids:
        artifact = store.load_artifact(artifact_id)
        path = store.artifact_path(artifact_id)
        if artifact.role == "analysis":
            archives.append(_json(path))
    members = derived.observations.get("analysis", {}).get("members", [])
    check("immutable_sampling_analysis_artifact", len(archives) == 1 and archives[0].get("members") == members)
    check("complete_five_member_table", len(members) == 5
          and {m["member_id"] for m in members} == set(aliases.values())
          and all(m.get("energy_eh") is None for m in members if m.get("status") != "qualified"))


def _grade_sampling_decisions(store, run, evidence, decisions, check):
    """Require a persisted adaptive decision, not merely the right final count."""
    decisions = sorted(decisions, key=lambda d: d["plan"]["version"])
    first_ids = {item["attempt"].step_id for item in evidence[:3]}
    initial = decisions[0]["plan"] if decisions else {"steps": []}
    science = [s for s in initial["steps"] if s["tool"] in {"orca.sp", "orca.opt"}]
    analyses = [s for s in initial["steps"] if s["tool"] == "analysis.finite_sampling"]
    initial_ok = (initial.get("version") == 1 and len(initial["steps"]) == 4
                  and len(science) == 3 and all(s["tool"] == "orca.sp" for s in science)
                  and {s["id"] for s in science} == first_ids and len(analyses) == 1
                  and {v.get("producer_step_id") for v in analyses[0].get("inputs", {}).values()} == first_ids
                  and len(analyses[0].get("inputs", {})) == 3)
    check("initial_sampling_plan_exactly_three_then_analysis", initial_ok)
    if len(evidence) == 3:
        no_append = all({s["id"] for s in d["plan"]["steps"] if s["tool"] in {"orca.sp", "orca.opt"}}
                        <= first_ids for d in decisions)
        check("sampling_append_follows_bound_analysis_feedback", initial_ok and no_append)
        return
    if len(evidence) != 4 or not initial_ok:
        check("sampling_append_follows_bound_analysis_feedback", False)
        return
    appended = evidence[3]["attempt"]
    introductions = [d for d in decisions[1:] if any(s["id"] == appended.step_id for s in d["plan"]["steps"])]
    valid = False
    first_sources = {e["result"].id: e for e in evidence[:3]}
    if introductions and getattr(appended, "plan_version", None) == introductions[0]["plan"]["version"]:
        for result_id in introductions[0].get("related_results", []):
            if result_id not in run.result_ids:
                continue
            prior = store.load_result(run.id, result_id)
            calls = [c for c in run.calls if c.id == prior.call_id and c.result_id == prior.id]
            if (prior.step_id != analyses[0]["id"] or prior.operation_status != "completed"
                    or "sampling" in prior.qualified_outputs or len(calls) != 1):
                continue
            call = calls[0]
            consumption = {k: v for k, v in call.consumption.items() if not k.startswith("_")}
            if (prior.source.get("consumption") != call.consumption or len(consumption) != 3
                    or {v.get("result_id") for v in consumption.values()} != set(first_sources)):
                continue
            bound = all(v.get("run_id") == run.id
                and v.get("attempt_id") == first_sources[v["result_id"]]["attempt"].id
                and v.get("artifact_hashes") == first_sources[v["result_id"]]["artifact_hashes"]
                and v.get("result_fingerprint") == fingerprint(first_sources[v["result_id"]]["result"])
                and v.get("rule_version") == "orca-hf-2" and v.get("status") == "qualified"
                for v in consumption.values())
            observation = prior.observations.get("analysis", {})
            facts = {e["result"].id: geometry_facts(e["files"]["geometry.xyz"]) for e in evidence[:3]}
            ordered = sorted(first_sources, key=lambda key: facts[key]["r01_angstrom"])
            values = [first_sources[key]["raw"]["energy_eh"] for key in ordered]
            left_gap, right_gap = values[0] - values[1], values[2] - values[1]
            span = facts[ordered[2]]["r01_angstrom"] - facts[ordered[0]]["r01_angstrom"]
            gap_observed = (observation.get("reason") == "span_too_wide"
                and observation.get("goal_satisfied") is False
                and observation.get("scientific_status") == "insufficient_evidence"
                and abs(observation.get("neighbor_span_angstrom", math.inf) - span) <= 1e-8
                and abs(observation.get("left_gap_eh", math.inf) - left_gap) <= 1e-10
                and abs(observation.get("right_gap_eh", math.inf) - right_gap) <= 1e-10)
            archives = [_json(store.artifact_path(a)) for a in prior.artifact_ids
                        if store.load_artifact(a).role == "analysis"]
            valid = bound and gap_observed and len(archives) == 1 and archives[0] == observation
            if valid:
                break
    check("sampling_append_follows_bound_analysis_feedback", valid)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("case", choices=CASES)
    parser.add_argument("run_id")
    parser.add_argument("--data-root", type=Path, default=PROJECT / "data/phase-b/agent")
    parser.add_argument("--metadata", type=Path,
                        help="immutable joint evaluation metadata; required for the raw methane control")
    args = parser.parse_args(argv)
    if args.case == "methane_opt_control" and args.metadata is None:
        parser.error("methane_opt_control requires --metadata to verify the raw input and optimized-energy target")
    metadata = _json(args.metadata) if args.metadata is not None else None
    if args.metadata is not None:
        if (not isinstance(metadata, dict) or metadata.get("run_id") != args.run_id
                or metadata.get("case") != args.case):
            parser.error("metadata Run/case identity differs from the requested evaluation")
        if args.case == "methane_opt_control" and (
                metadata.get("input_form") != "raw_text"
                or not isinstance(metadata.get("raw_bundle_path"), str)
                or not metadata["raw_bundle_path"]
                or not isinstance(metadata.get("raw_bundle_sha256"), str)
                or re.fullmatch(r"[a-f0-9]{64}", metadata["raw_bundle_sha256"]) is None):
            parser.error("methane_opt_control metadata must identify its raw input bundle and SHA-256")
    # Construction is avoided here: grading must not create absent Store dirs.
    from orca_agent.store import Store
    if not args.data_root.is_dir():
        raise ValueError("existing Store required")
    store = object.__new__(Store)
    store.root = args.data_root.resolve()
    if metadata is not None and metadata.get("category") != store.load_run(args.run_id).batch_category:
        parser.error("metadata category differs from the persisted Run")
    print(json.dumps(grade_joint(store, args.run_id, args.case, metadata=metadata), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
