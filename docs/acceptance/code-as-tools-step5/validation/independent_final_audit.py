"""Read archived evidence independently; never import or execute product/task code.

Run only after the coordinator has finished. The only write is the requested
JSON audit outside the Store. Missing evidence is reported as unknown, not pass.
"""

import argparse
import collections
import csv
import datetime
import hashlib
import io
import itertools
import json
import math
from pathlib import Path
import re


NUMBER = r"[-+]?\d+(?:\.\d+)?(?:[EeDd][-+]?\d+)?"
CONDITIONS = ("method", "basis", "charge", "multiplicity", "electronic_state", "environment")
ENERGY_COLUMNS = ("orca_energy_hartree", "energy_hartree", "energy_Eh", "energy_eh")
DIFFERENCE_COLUMNS = ("relative_energy_kcal_mol", "relative_energy_kJ_mol", "delta_energy_kcal_mol",
                      "relative_energy_kj_mol", "delta_energy_kJ_mol", "delta_energy_kj_mol",
                      "delta_e_kcal_mol", "delta_e_kj_mol", "relative_energy_ev", "relative_energy_eV")
EH_TO_KJ_MOL = 2625.4996394799
EH_TO_EV = 27.211386245988


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def number(value):
    try:
        result = float(value)
    except (ValueError, TypeError):
        return None
    return result if math.isfinite(result) else None


class Auditor:
    def __init__(self, root, run_id, rules):
        self.root = root.resolve()
        self.run_path = self.root / "runs" / run_id
        self.sources, self.issues = {}, []
        self.rules = rules
        self.run_raw = (self.run_path / "run.json").read_bytes()
        self.run = json.loads(self.run_raw)

    def artifact(self, identifier, expected=None):
        if identifier in self.sources:
            source = self.sources[identifier]
            if expected and expected != source["sha256"]:
                raise ValueError("different source hash binding: " + identifier)
            raw = Path(source["path"]).read_bytes()
            if digest(raw) != source["sha256"]:
                raise ValueError("source changed during audit: " + identifier)
            return raw
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,96}", identifier):
            raise ValueError("invalid artifact identifier")
        meta_path = self.root / "artifacts" / identifier / "artifact.json"
        meta = json.loads(meta_path.read_bytes())
        path = (self.root / meta["path"]).resolve()
        if not path.is_relative_to(self.root) or meta["id"] != identifier:
            raise ValueError("artifact path/identity differs")
        if path.stat().st_size > 64 * 1024 * 1024:
            raise ValueError("audit read exceeds 64 MiB; requires explicit larger-file audit")
        raw = path.read_bytes()
        if len(raw) != meta["size"] or digest(raw) != meta["sha256"] or expected not in {None, meta["sha256"]}:
            raise ValueError("artifact hash/size differs: " + identifier)
        self.sources[identifier] = {"artifact_id": identifier, "path": str(path), "sha256": digest(raw),
            "role": meta["role"], "run_id": meta.get("run_id"), "attempt_id": meta.get("attempt_id")}
        return raw

    def revision(self, kind):
        version = self.run[kind + "_version"]
        activated = []
        for summary in self.run.get("decisions", []):
            if summary.get(kind + "_version") != version:
                continue
            raw = (self.run_path / "decisions" / (summary["id"] + ".json")).read_bytes()
            record = json.loads(raw)
            if isinstance(record.get(kind), dict):
                activated.append(record[kind])
        if activated:
            if any(item != activated[0] for item in activated):
                raise ValueError("activated revisions conflict")
            return activated[0]
        return json.loads((self.run_path / (kind + "-revisions") / (str(version) + ".json")).read_bytes())

    def native(self, call):
        row = {key: call.get(key) for key in ("request_key", "state", "parent_attempt_id", "actual_conditions")}
        row["eligible_as_energy_evidence"] = False
        try:
            reference = call["code"]
            inp = self.artifact(reference["artifact_id"], reference["sha256"]).decode("utf-8")
            keywords = [line.strip()[1:].strip() for line in inp.splitlines() if line.lstrip().startswith("!")]
            geometries = re.findall(r"^\s*\*\s+xyzfile\s+(-?\d+)\s+(\d+)\s+(\S+)", inp, re.M | re.I)
            row.update(native_input_artifact=reference["artifact_id"], native_input_keywords=keywords,
                       native_input_geometry_declarations=geometries)
            dependencies = []
            for dependency in call.get("inputs", {}).get("entries", []):
                raw = self.artifact(dependency["artifact_id"], dependency["sha256"])
                if dependency.get("target_path", "").lower().endswith(".xyz"):
                    elements, xyz = xyz_data(raw)
                    dependencies.append({"artifact_id": dependency["artifact_id"], "target_path": dependency["target_path"],
                        "formula": dict(collections.Counter(elements)), "ordered_elements": elements,
                        "finite_coordinates": all(math.isfinite(v) for point in xyz for v in point), "sha256": digest(raw)})
            row["input_geometries"] = dependencies
            outputs = []
            for identifier in call.get("artifact_ids", []):
                raw = self.artifact(identifier)
                if Path(self.sources[identifier]["path"]).name == "stdout.out":
                    outputs.append((identifier, raw.decode("utf-8", errors="replace")))
            if len(outputs) != 1:
                raise ValueError("native stdout missing or ambiguous")
            identifier, text = outputs[0]
            energies = [(index, float(match.group(1).replace("D", "E")))
                        for index, line in enumerate(text.splitlines(), 1)
                        if (match := re.search(r"FINAL SINGLE POINT ENERGY\s+(" + NUMBER + r")", line))]
            converged = [index for index, line in enumerate(text.splitlines(), 1)
                         if "SCF CONVERGED AFTER" in line.upper()]
            normal = [index for index, line in enumerate(text.splitlines(), 1)
                      if "ORCA TERMINATED NORMALLY" in line.upper()]
            failed = [index for index, line in enumerate(text.splitlines(), 1)
                      if "SCF NOT CONVERGED" in line.upper() or "SCF DID NOT CONVERGE" in line.upper()]
            row.update(stdout_artifact=identifier, energies_Eh=energies, SCF_convergence_lines=converged,
                       normal_termination_lines=normal, SCF_failure_lines=failed)
            # This audit handles the declared single-point ranking. Multiple
            # stages require an explicit independent audit, never newest-value selection.
            row["eligible_as_energy_evidence"] = bool(call["state"] == "completed" and len(energies) == 1
                and converged and converged[-1] < energies[0][0] and normal and normal[-1] > energies[0][0] and not failed)
            row["energy_Eh"] = energies[0][1] if len(energies) == 1 else None
            row["input_charge_multiplicity_matches_receipt"] = bool(len(geometries) == 1
                and [int(geometries[0][0]), int(geometries[0][1])] == [call["actual_conditions"].get("charge"), call["actual_conditions"].get("multiplicity")])
            row["input_keywords_echoed_in_stdout"] = all(line in text for line in keywords)
        except (KeyError, ValueError, OSError, TypeError) as exc:
            row["unknown_or_error"] = str(exc)
        return row


def xyz_data(raw):
    lines = raw.decode("utf-8").splitlines()
    atoms = [line.split() for line in lines[2:] if line.strip()]
    if len(atoms) != int(lines[0]):
        raise ValueError("XYZ atom count differs")
    return [atom[0] for atom in atoms], [tuple(map(float, atom[1:4])) for atom in atoms]


def sdf_data(raw):
    candidates = {}
    for block in raw.decode("utf-8").split("$$$$"):
        if not block.strip():
            continue
        lines = block.strip().splitlines()
        if "V2000" not in lines[3]:
            raise ValueError("independent auditor requires V2000 SDF")
        count, bonds = int(lines[3][:3]), int(lines[3][3:6])
        atoms = [line.split() for line in lines[4:4 + count]]
        candidates[lines[0]] = {"elements": [atom[3] for atom in atoms],
            "xyz": [tuple(map(float, atom[:3])) for atom in atoms],
            "bonds": [(int(line[:3]) - 1, int(line[3:6]) - 1, int(line[6:9]))
                      for line in lines[4 + count:4 + count + bonds]]}
    return candidates


def inspect(auditor):
    run = auditor.run
    request = auditor.revision("request")
    results = [json.loads((auditor.run_path / "results" / (identifier + ".json")).read_bytes())
               for identifier in run.get("result_ids", [])]
    attempts = {item["id"]: item for item in run.get("attempts", [])}
    native = [auditor.native(call) for call in run.get("internal_calls", [])]
    initial, identities = [], []
    for bindings in run.get("input_bindings", {}).values():
        geometry = bindings.get("prepared_geometry")
        if geometry:
            raw = auditor.artifact(geometry["artifact_id"], geometry["sha256"])
            elements, xyz = xyz_data(raw)
            initial.append({"artifact_id": geometry["artifact_id"], "sha256": digest(raw),
                "formula": dict(collections.Counter(elements)), "ordered_elements": elements,
                "finite_coordinates": all(math.isfinite(v) for point in xyz for v in point)})
        identity = bindings.get("resolved_identity")
        if identity:
            identities.append(json.loads(auditor.artifact(identity["artifact_id"], identity["sha256"])))
    expected_elements = identities[0].get("elements") if len(identities) == 1 else None
    expected_bonds = {tuple(sorted(edge)) for edge in identities[0]["graph"]["bonds"]} if len(identities) == 1 else None
    task_results = []
    for result in results:
        if not result.get("attempt_id"):
            continue
        entry = {"result_id": result["id"], "attempt_id": result["attempt_id"], "operation_status": result["operation_status"],
                 "qualified_outputs": result.get("qualified_outputs", {}), "manifest": None, "structures": [],
                 "XYZ_structures": [], "tables": [], "diagnostics": result.get("diagnostics", []),
                 "accepted_observation_record_count": len(result.get("observation_records", []))}
        task_results.append(entry)
        xyz_candidates = {}
        attempt = attempts.get(result["attempt_id"], {})
        reference = attempt.get("frozen_step", {}).get("parameters", {}).get("code")
        if reference:
            auditor.artifact(reference["artifact_id"], reference["sha256"])
            entry["generated_script"] = reference
        for task_path, reference in result.get("source", {}).get("files", {}).items():
            if not task_path.startswith("task/"):
                continue
            raw = auditor.artifact(reference["artifact_id"], reference["sha256"])
            if task_path.endswith("stderr.txt"):
                entry["task_stderr"] = {"artifact_id": reference["artifact_id"],
                    "text_excerpt": raw[:16000].decode("utf-8", errors="replace"), "bytes": len(raw)}
            if task_path == "task/observations.json":
                entry["manifest"] = json.loads(raw)
                entry["manifest_source_lists_nonempty"] = all(bool(item.get("sources"))
                    for item in entry["manifest"].get("observations", []))
                entry["declared_generated_candidate_counts"] = [item["value"][name]
                    for item in entry["manifest"].get("observations", []) if isinstance(item.get("value"), dict)
                    for name in ("n_embedded", "generated_conformers", "generated_candidates")
                    if type(item["value"].get(name)) is int]
            if task_path.lower().endswith(".xyz"):
                elements, xyz = xyz_data(raw)
                radii = {"C": 0.76, "H": 0.31}
                inferred = {tuple(sorted((i, j))) for i in range(len(elements)) for j in range(i)
                    if elements[i] in radii and elements[j] in radii
                    and 0.4 < math.dist(xyz[i], xyz[j]) <= 1.25 * (radii[elements[i]] + radii[elements[j]])}
                entry["XYZ_structures"].append({"artifact_id": reference["artifact_id"], "task_path": task_path,
                    "formula": dict(collections.Counter(elements)), "ordered_elements_match_initial": elements == expected_elements,
                    "finite_coordinates": all(math.isfinite(v) for point in xyz for v in point),
                    "distance_inferred_bonds_match_identity": expected_bonds is not None and inferred == expected_bonds,
                    "bond_check_method": "Independent distance heuristic: 0.4 < d <= 1.25*(covalent radii sum), C=0.76A H=0.31A; not an optimized-geometry or electronic-state check."})
                if "initial" not in Path(task_path).stem.lower() and "prepared" not in Path(task_path).stem.lower():
                    xyz_candidates[task_path] = xyz
            if task_path.lower().endswith(".sdf"):
                candidates = sdf_data(raw)
                checks = []
                for name, candidate in candidates.items():
                    checks.append({"candidate_id": name, "formula": dict(collections.Counter(candidate["elements"])),
                        "ordered_elements_match_initial": candidate["elements"] == expected_elements,
                        "bond_graph_matches_identity": expected_bonds is not None and
                            {tuple(sorted((i, j))) for i, j, order in candidate["bonds"]} == expected_bonds and
                            all(order == 1 for i, j, order in candidate["bonds"]),
                        "finite_coordinates": all(math.isfinite(v) for point in candidate["xyz"] for v in point)})
                differences = {a + "/" + b: max(abs(math.dist(candidates[a]["xyz"][i], candidates[a]["xyz"][j])
                    - math.dist(candidates[b]["xyz"][i], candidates[b]["xyz"][j]))
                    for i in range(20) for j in range(i)) for a, b in itertools.combinations(candidates, 2)}
                entry["structures"].append({"artifact_id": reference["artifact_id"], "task_path": task_path,
                    "count": len(candidates), "candidate_checks": checks, "distinct_internal_geometry_pairs":
                    sum(value > 1e-3 for value in differences.values()), "pairwise_max_distance_differences_A": differences})
            if task_path.lower().endswith(".csv"):
                rows = list(csv.DictReader(io.StringIO(raw.decode("utf-8-sig"))))
                table = {"artifact_id": reference["artifact_id"], "task_path": task_path, "rows": rows, "energy_checks": []}
                entry["tables"].append(table)
                if not rows:
                    continue
                energy_column = next((name for name in ENERGY_COLUMNS if name in rows[0]), None)
                energies = [number(row.get(energy_column)) for row in rows] if energy_column else []
                finite_energies = [value for value in energies if value is not None]
                minimum = min(finite_energies) if finite_energies else None
                table.update(energy_column=energy_column, energy_count=len(finite_energies),
                    minimum_tabulated_Eh=minimum, energy_rows_ascending=finite_energies == sorted(finite_energies),
                    statuses=dict(collections.Counter(row.get("orca_status", row.get("status", "unknown")) for row in rows)),
                    all_failed_excluded_have_reason=all(bool(row.get("exclusion_reason") or row.get("reason") or row.get("error"))
                        for row in rows if row.get("orca_status", row.get("status")) in {"failed", "not_selected", "excluded"}),
                    reason_check_scope="CSV fields only. Failure reasons in a separate observation manifest are retained there and reviewed separately.")
                for row in rows:
                    value = number(row.get(energy_column)) if energy_column else None
                    if value is None:
                        continue
                    candidate = row.get("candidate_id", row.get("member_id", ""))
                    key = row.get("orca_request_key") or row.get("request_key")
                    matches = [item for item in native if item.get("request_key") == key] if key else [
                        item for item in native if candidate and re.search(r"(?<![A-Za-z0-9])" + re.escape(candidate)
                            + r"(?![A-Za-z0-9])", item.get("request_key", ""))]
                    check = {"candidate_id": candidate, "table_energy_Eh": value,
                             "raw_call_matches": [item["request_key"] for item in matches]}
                    if len(matches) == 1 and matches[0].get("energy_Eh") is not None:
                        check["source_resolution"] = "explicit_request_key" if key else "unique_candidate_id_in_request_key"
                        check["energy_difference_from_raw_Eh"] = value - matches[0]["energy_Eh"]
                        check["energy_matches_raw_within_tolerance"] = abs(check["energy_difference_from_raw_Eh"]) <= auditor.rules["energy_tolerance_Eh"]
                        check["raw_SCF_termination_eligible"] = matches[0]["eligible_as_energy_evidence"]
                    else:
                        check["unknown"] = "Raw stdout binding missing or ambiguous; no value is assumed."
                    for column in DIFFERENCE_COLUMNS:
                        observed = number(row.get(column))
                        if observed is not None and minimum is not None:
                            expected_kj = (value - minimum) * EH_TO_KJ_MOL
                            observed_kj = (observed * EH_TO_KJ_MOL / EH_TO_EV if column.lower().endswith("ev")
                                           else observed * 4.184 if "kcal" in column else observed)
                            check[column] = {"observed": observed, "recomputed_kJ_mol": expected_kj,
                                "difference_kJ_mol": observed_kj - expected_kj, "within_tolerance":
                                abs(observed_kj - expected_kj) <= auditor.rules["difference_tolerance_kJ_mol"]}
                    table["energy_checks"].append(check)
        entry["XYZ_candidate_count"] = len(xyz_candidates)
        entry["XYZ_pairwise_internal_distance_differences_A"] = {a + "/" + b:
            max(abs(math.dist(xyz_candidates[a][i], xyz_candidates[a][j])
                - math.dist(xyz_candidates[b][i], xyz_candidates[b][j]))
                for i in range(len(xyz_candidates[a])) for j in range(i))
            for a, b in itertools.combinations(xyz_candidates, 2)
            if len(xyz_candidates[a]) == len(xyz_candidates[b]) and len(xyz_candidates[a]) > 1}
        stored_count = max([entry["XYZ_candidate_count"], *[item["count"] for item in entry["structures"]]])
        declared_counts = entry.get("declared_generated_candidate_counts", [])
        entry["declared_vs_archived_candidate_count"] = {"declared": declared_counts,
            "archived_coordinate_records": stored_count,
            "all_declared_candidates_have_archived_coordinates": all(count <= stored_count for count in declared_counts)
                if declared_counts else None,
            "scope": "Each task Result separately; initial XYZ is excluded. Multiple coordinate formats are treated as copies, not added together."}
    eligible = [item for item in native if item["eligible_as_energy_evidence"]]
    actual_protocols = [{name: item.get("actual_conditions", {}).get(name) for name in CONDITIONS} for item in eligible]
    common = bool(actual_protocols and all(item == actual_protocols[0] for item in actual_protocols))
    request_conditions = {name: request.get("conditions", {}).get(name, request.get(name)) for name in CONDITIONS}
    current_report = None
    receipts = run.get("terminal_deliveries", []) + run.get("fallback_report_receipts", [])
    for receipt in reversed(receipts):
        if receipt.get("report_status") == "rendered" and receipt.get("report_path"):
            path = (auditor.root / receipt["report_path"]).resolve()
            if not path.is_relative_to(auditor.root):
                raise ValueError("report path escapes Store")
            raw = path.read_bytes()
            current_report = {"path": str(path), "sha256": digest(raw),
                "matches_receipt_hash": digest(raw) == receipt.get("report_sha256"), "text": raw.decode("utf-8")}
            break
    return {"run_id": run["id"], "run_state": run["state"], "run_snapshot_sha256": digest(auditor.run_raw),
        "run_snapshot_changed_during_audit": (auditor.run_path / "run.json").read_bytes() != auditor.run_raw,
        "request_conditions": request_conditions, "goals": request.get("goals", []),
        "original_initial_geometries": initial, "native_calls": native,
        "eligible_native_energy_count": len(eligible), "uniform_actual_conditions": common,
        "actual_condition_sets": actual_protocols,
        "actual_matches_requested_conditions": bool(common and all(actual_protocols[0].get(name) == value
            for name, value in request_conditions.items() if value is not None)),
        "task_results": task_results,
        "generated_python_results_all_have_empty_qualified_outputs": all(not item["qualified_outputs"] for item in task_results),
        "goal_status": run.get("goal_status"), "delivery_status": run.get("delivery_status"),
        "model_explanation_receipts": run.get("terminal_deliveries", []), "deterministic_report": current_report,
        "budget_usage": run.get("usage"), "diagnostics": run.get("diagnostics"),
        "full_task_pass": None, "scientific_qualification_granted": False,
        "interpretation": "Raw independent audit facts only. Review all unknowns, comparisons, finite-scope wording and report content before an acceptance conclusion."}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--store", type=Path, default=Path(__file__).parent / "live-store")
    parser.add_argument("--run", required=True, help="Exact coordinator Run ID; never infer the newest Run.")
    parser.add_argument("--rules", type=Path, default=Path(__file__).parent / "independent-check-rules.json")
    parser.add_argument("--output", type=Path, default=Path(__file__).parent / "independent-final-005.json")
    args = parser.parse_args()
    if args.output.resolve().is_relative_to(args.store.resolve()):
        raise ValueError("Audit output must be outside the Store/Run.")
    rules_raw = args.rules.read_bytes()
    auditor = Auditor(args.store, args.run, json.loads(rules_raw))
    report = {"created_at_utc": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "audit_kind": "independent_read_only_raw_evidence", "rules_sha256": digest(rules_raw),
        "generated_code_executed": False, "product_code_imported": False,
        "ORCA_forcefield_or_API_executions": 0, "Run_or_product_modified": False}
    try:
        report["facts"] = inspect(auditor)
    except (KeyError, ValueError, TypeError, OSError, IndexError) as exc:
        report["audit_error"] = str(exc)
        report["full_task_pass"] = None
    report["source_artifacts"] = list(auditor.sources.values())
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"output": str(args.output.resolve()), "sha256": digest(args.output.read_bytes()),
                      "audit_error": report.get("audit_error"), "full_task_pass": None}, ensure_ascii=False))


if __name__ == "__main__":
    main()
