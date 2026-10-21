"""Independent read-only T5 audit, invoked only after an explicit settled notice.

No product imports, generated-code execution, subprocesses, network or polling.
Default is inspection only. --write appends a new audit, never replaces one.
Manual report/source interpretation is supplied separately with --review-json.
"""
import argparse
import hashlib
import json
import re
from collections import Counter
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from native_raw_sources import discover_native_sources

ROOT = Path(__file__).resolve().parents[2]
FREEZE = ROOT / "docs/acceptance/code-as-tools-step7-refreeze-4/freeze"
NUMBER = r"[-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[EeDd][-+]?\d+)?"
CONVERSION = Decimal("2625.4996394799")
MANUAL_ITEMS = ("method_basis_and_preexecution_rationale", "actual_solvent_settings_and_source",
                "delivered_table_energies_delta_sign_and_precision", "generated_analysis_source_and_provenance",
                "electronic_difference_not_hydration_free_energy", "seed_and_geometry_reproducibility_limits")


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def decimal(value):
    answer = Decimal(value.replace("D", "E").replace("d", "e"))
    assert answer.is_finite()
    return answer


def xyz(raw):
    lines = raw.decode("utf-8").splitlines()
    count = int(lines[0])
    rows = [line.split() for line in lines[2:] if line.strip()]
    assert len(rows) == count and all(len(row) == 4 for row in rows)
    assert all(re.fullmatch(r"[A-Z][a-z]?", row[0]) for row in rows)
    for row in rows:
        for value in row[1:]:
            decimal(value)
    return rows


def exact_coordinates(left, right):
    return len(left) == len(right) and all(a[0] == b[0]
        and all(decimal(x) == decimal(y) for x, y in zip(a[1:], b[1:], strict=True))
        for a, b in zip(left, right, strict=True))


def input_summary(text):
    """Conservative text comparison; unfamiliar geometry remains unverified."""
    # Normalize only the parsing view; artifact bytes/hashes remain original.
    text = "\n".join(text.splitlines())
    lines = [line.split("#", 1)[0].strip() for line in text.splitlines()]
    simple = [token for line in lines if line.startswith("!") for token in line[1:].split()]
    xyzfile = re.search(r"(?im)^\s*\*\s*xyzfile\s+(-?\d+)\s+(\d+)\s+([^\r\n]+)$", text)
    inline = re.search(r"(?ims)^\s*\*\s*xyz\s+(-?\d+)\s+(\d+)\s*\n(.*?)^\s*\*\s*$", text)
    geometry, charge, multiplicity, filename = None, None, None, None
    if xyzfile:
        charge, multiplicity = int(xyzfile[1]), int(xyzfile[2])
        filename = xyzfile[3].strip().strip('"')
    elif inline:
        charge, multiplicity = int(inline[1]), int(inline[2])
        geometry = [line.split() for line in inline[3].splitlines() if line.strip()]
        assert all(len(row) == 4 for row in geometry)
        for row in geometry:
            for value in row[1:]:
                decimal(value)
    cpcm_tokens = [token for token in simple if re.fullmatch(r"C[-]?PCM(?:\([^)]*\))?", token, re.I)]
    water_token = any(re.fullmatch(r"C[-]?PCM\(water\)", token, re.I) for token in cpcm_tokens)
    blocks, block_lines, current = [], [], None
    body = []
    geometry_body = False
    for line in lines:
        if not line or line.startswith("!"):
            continue
        if current:
            block_lines.append(line)
            if re.search(r"\bend\s*$", line, re.I):
                blocks.append({"name": current, "lines": block_lines})
                current, block_lines = None, []
            continue
        opening = re.match(r"%([a-zA-Z][a-zA-Z0-9_]*)\b", line)
        if opening and opening[1].lower() in {"cpcm", "pal"}:
            current, block_lines = opening[1].lower(), [line]
            if re.search(r"\bend\s*$", line, re.I):
                blocks.append({"name": current, "lines": block_lines})
                current, block_lines = None, []
            continue
        if re.match(r"%maxcore\b", line, re.I):
            continue
        if re.match(r"\*\s*xyzfile\b", line, re.I):
            continue
        if re.match(r"\*\s*xyz\b", line, re.I):
            geometry_body = True
            continue
        if geometry_body:
            if line == "*":
                geometry_body = False
            continue
        body.append(re.sub(r"\s+", " ", line).casefold())
    assert current is None, "Unclosed supported input block requires manual inspection"
    cpcm_blocks = [block for block in blocks if block["name"] == "cpcm"]
    return {"charge": charge, "multiplicity": multiplicity, "xyz_filename": filename,
        "inline_coordinates": geometry, "simple_keywords": simple, "solvent_tokens": cpcm_tokens,
        "solvent_blocks": cpcm_blocks, "water_explicit_in_simple_input": water_token,
        "solvent_input_present": bool(cpcm_tokens or cpcm_blocks),
        "single_point_only_in_input": not any(re.fullmatch(
            r"(?:.*opt.*|.*freq.*|scan|md|engrad|neb.*)", token, re.I) for token in simple)
            and not re.search(r"(?im)^\s*%geom\b", text),
        "scientific_text_without_geometry_resources_or_solvent": {
            "simple_keywords": sorted(token.casefold() for token in simple if token not in cpcm_tokens),
            "remaining_body_lines": body}}


def stdout_summary(text):
    lines = text.splitlines()
    energy, markers, coordinates, solvent_lines = [], [], [], []
    for i, line in enumerate(lines):
        match = re.search(r"\bFINAL SINGLE POINT ENERGY\s+(" + NUMBER + ")", line)
        if match:
            decimal(match[1])
            energy.append({"line": i + 1, "hartree": match[1]})
        if any(token in line for token in ("ORCA TERMINATED NORMALLY", "SCF CONVERGED AFTER",
            "SCF NOT CONVERGED", "SCF DID NOT CONVERGE", "THE OPTIMIZATION HAS CONVERGED")):
            markers.append({"line": i + 1, "text": line.strip()})
        if re.search(r"C-?PCM|Solvent\s*[:.=]|Dielectric|Epsilon|Cavity|Refrac", line, re.I):
            solvent_lines.append({"line": i + 1, "text": line.strip()[:400]})
        if "CARTESIAN COORDINATES (ANGSTROEM)" in line:
            rows = []
            for j in range(i + 1, len(lines)):
                match = re.fullmatch(r"\s*([A-Z][a-z]?)\s+(" + NUMBER + r")\s+(" + NUMBER
                                     + r")\s+(" + NUMBER + r")\s*", lines[j])
                if match:
                    rows.append(list(match.groups()))
                elif rows or j > i + 3:
                    break
            if rows:
                coordinates.append({"header_line": i + 1, "rows": rows})
    water_lines = [row for row in solvent_lines if re.search(r"\bsolvent\b.*\bwater\b", row["text"], re.I)]
    return {"energy_records": energy, "termination_and_scf_lines": markers,
        "normal_termination": any("ORCA TERMINATED NORMALLY" in row["text"] for row in markers),
        "scf_converged": any("SCF CONVERGED AFTER" in row["text"] for row in markers),
        "scf_failed": any(re.search(r"SCF (?:NOT CONVERGED|DID NOT CONVERGE)", row["text"]) for row in markers),
        "optimization_converged": any("THE OPTIMIZATION HAS CONVERGED" in row["text"] for row in markers),
        "coordinate_blocks": coordinates, "solvent_lines": solvent_lines[:80],
        "water_confirmed_by_solvent_label": bool(water_lines),
        "cpcm_output_marker": bool(re.search(r"C-?PCM\s+SOLVATION\s+MODEL", text, re.I))}


class Evidence:
    def __init__(self, base, run):
        self.base, self.data, self.run = base, base / "data", run
        self.hashes, self.files = {}, []

    def retain(self, path):
        raw = path.read_bytes()
        hashed = digest(raw)
        if path in self.hashes:
            assert self.hashes[path] == hashed, "Source changed during audit"
        self.hashes[path] = hashed
        return {"path": path.relative_to(ROOT).as_posix() if path.is_relative_to(ROOT) else str(path),
                "sha256": hashed, "bytes": len(raw)}

    def artifact(self, result, artifact_id, key, expected=None):
        assert artifact_id in result["artifact_ids"]
        metadata_path = self.data / "artifacts" / artifact_id / "artifact.json"
        metadata = json.loads(metadata_path.read_bytes())
        assert metadata["id"] == artifact_id and metadata["run_id"] == self.run["id"]
        assert metadata.get("attempt_id") is None or metadata["attempt_id"] == result.get("attempt_id")
        path = (self.data / metadata["path"]).resolve()
        assert path.is_relative_to(self.data.resolve())
        raw = path.read_bytes()
        assert len(raw) == metadata["size"] and digest(raw) == metadata["sha256"]
        assert expected is None or digest(raw) == expected
        row = {"result_id": result["id"], "attempt_id": result.get("attempt_id"),
            "artifact_id": artifact_id, "source_file_key": key, **self.retain(path),
            "metadata": self.retain(metadata_path), "owner_and_hash_verified": True}
        self.files.append(row)
        return row, raw

    def unchanged(self):
        return all(digest(path.read_bytes()) == expected for path, expected in self.hashes.items())


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--candidate", required=True)
    parser.add_argument("--review-json", type=Path)
    parser.add_argument("--write", action="store_true")
    args = parser.parse_args()
    assert re.fullmatch(r"a[0-9]{3}", args.candidate)
    base = ROOT / "data/live-repair/attempts" / args.candidate
    assert (base / "outcome.json").is_file(), "Wait for root's settled notice; no polling"
    start = json.loads((base / "start.json").read_bytes())
    outcome = json.loads((base / "outcome.json").read_bytes())
    capture = json.loads((base / "attempt.json").read_bytes())
    assert start["task"] == capture["task"] == outcome["task"] == "T5"
    directory = base / "data/runs" / start["run_id"]
    run = json.loads((directory / "run.json").read_bytes())
    assert run["id"] == start["run_id"] == outcome["run_id"]
    assert run["state"] == outcome["state"] and run["usage"] == outcome["usage"]
    assert not outcome["unsettled_execution"]
    assert set(outcome["unknown_fields"]) <= {"model_tokens"}, "Unknown scientific execution must be reconciled first"
    assert not any(row["state"] in {"intent", "reserved", "running", "unknown"}
        for key in ("attempts", "calls", "internal_calls") for row in run[key])
    destination = base / "independent-raw-check-v3.json"
    assert not destination.exists(), "Do not overwrite a previous audit"
    evidence = Evidence(base, run)
    for path in (base / "data").rglob("*"):
        if path.is_file() and path.suffix != ".lock":
            evidence.retain(path)
    for name, identity in capture["source_files"].items():
        assert evidence.retain(Path(capture["snapshot"]) / name)["sha256"] == identity["sha256"]
    authorities = {key: evidence.retain(path) for key, path in {
        "original": base / "original.txt", "capture": base / "attempt.json", "start": base / "start.json",
        "outcome": base / "outcome.json", "configuration": base / "configuration.json",
        "task_and_tolerances": FREEZE / "public-tasks.json", "scoring": FREEZE / "scoring.md",
        "exit_checklist": ROOT / "data/live-repair/remaining-six-task-exit-checklist.json"}.items()}
    authority = json.loads((FREEZE / "public-tasks.json").read_bytes())
    task = next(row for row in authority["tasks"] if row["id"] == "T5")
    expected_original = task["original_text"] + authority["seed_policy"]["separator"] + authority["seed_policy"]["seed_suffix_template"].format(seed=1701)
    actual_original = (base / "original.txt").read_bytes().decode("utf-8")
    assert actual_original == expected_original
    assert authorities["original"]["sha256"] == capture["original_sha256"]
    attempts = {row["id"]: row for row in run["attempts"]}
    calls = {row["id"]: row for row in run["calls"]}
    results, preparation, native, programs, data_products = [], [], [], [], []
    for result_id in run["result_ids"]:
        path = directory / "results" / f"{result_id}.json"
        result = json.loads(path.read_bytes())
        assert result["id"] == result_id and result["run_id"] == run["id"]
        producer = attempts.get(result.get("attempt_id")) or calls.get(result.get("call_id"))
        assert producer and producer["result_id"] == result_id
        results.append({"id": result_id, "operation_status": result["operation_status"], "record": evidence.retain(path)})
        prepared = result.get("qualified_outputs", {}).get("prepared_geometry")
        if prepared:
            row, raw = evidence.artifact(result, prepared["artifact_id"], "prepared_geometry")
            source = result["source"]
            preparation.append({"source": row, "coordinates": xyz(raw), **{key: source.get(key) for key in
                ("smiles", "elements", "atom_mapping", "charge", "multiplicity", "generator", "random_seed", "generation_reproducibility", "versions")}})
        bound = {}
        for key, binding in result.get("source", {}).get("files", {}).items():
            bound[key] = evidence.artifact(result, binding["artifact_id"], key, binding["sha256"])
        for key, (row, raw) in bound.items():
            if key.endswith(".py"):
                text = raw.decode("utf-8")
                programs.append({"source": row, "not_executed_by_auditor": True,
                    "arithmetic_seed_source_lines": [{"line": i + 1, "text": line[:450]}
                        for i, line in enumerate(text.splitlines()) if re.search(r"2625|seed|1701|FINAL SINGLE|water|gas|delta|sha256", line, re.I)][:60]})
            if key.startswith("task/") and key.endswith((".csv", ".tsv", ".json", ".md", ".txt", ".xyz")):
                data_products.append(row)
    discovered = discover_native_sources(evidence.data, run, evidence.retain)
    for source in discovered:
        bound = source["files"]
        stdout_item = bound.get("task/stdout.out")
        input_item = bound.get("task/job.inp")
        parsed_input = input_summary(input_item[1].decode("utf-8")) if input_item else None
        geometry = parsed_input.get("inline_coordinates") if parsed_input else None
        xyz_ref = None
        if parsed_input and parsed_input["xyz_filename"]:
            name = parsed_input["xyz_filename"]
            assert not Path(name).is_absolute() and ".." not in name.replace("\\", "/").split("/")
            xyz_item = bound.get("task/" + name.replace("\\", "/"))
            if xyz_item:
                xyz_ref, xyz_raw = xyz_item
                geometry = xyz(xyz_raw)
        execution = source["execution"]
        output = stdout_summary(stdout_item[1].decode("utf-8")) if stdout_item else stdout_summary("")
        input_solvent = parsed_input and parsed_input["solvent_input_present"]
        label = "water" if input_solvent and output["water_confirmed_by_solvent_label"] else "gas" if parsed_input and not input_solvent and not output["cpcm_output_marker"] else "unverified_environment"
        native.append({"result_id": source["result"]["id"], "attempt_id": source["parent"]["id"],
            "request_key": source["request_key"], "state": source["state"], "started": source["started"],
            "outer_operation_status": source["outer_operation_status"],
            "native_identity": source["native_identity"], "source_authenticated": source["source_authenticated"],
            "actual_started_confirmed": source["actual_started_confirmed"],
            "verification_issues": source["verification_issues"], "source_record_refs": source["source_record_refs"],
            "created_at": source["receipt"].get("created_at"), "environment_classification": label,
            "stdout": stdout_item[0] if stdout_item else None, "input": input_item[0] if input_item else None,
            "source_input": bound.get("task/source-input.inp", (None,))[0],
            "execution_record": bound.get("execution.json", (None,))[0],
            "actual_start_record": bound.get("actual-start.json", (None,))[0],
            "actual_start_status": execution.get("actual_start_status"), "exit_code": execution.get("exit_code"),
            "actual_start_evidence": (source["receipt"].get("execution_handle") or {}).get("actual_start_evidence"),
            "launch_receipt_sha256": (source["receipt"].get("execution_handle") or {}).get("launch_receipt_sha256"),
            "parsed_input": parsed_input, "geometry_source": xyz_ref, "input_coordinates": geometry,
            "raw_output": output})

    checks = []
    def check(name, ok, detail):
        checks.append({"name": name, "status": "pass" if ok else "unverified", "detail": detail})
    successful = [row for row in native if row["state"] == "completed" and row["source_authenticated"] and row["actual_started_confirmed"]
        and row["raw_output"]["normal_termination"] and row["raw_output"]["scf_converged"]
        and not row["raw_output"]["scf_failed"] and len(row["raw_output"]["energy_records"]) == 1
        and row["parsed_input"] and row["parsed_input"]["single_point_only_in_input"]
        and not row["raw_output"]["optimization_converged"]]
    gas = [row for row in successful if row["environment_classification"] == "gas"]
    water = [row for row in successful if row["environment_classification"] == "water"]
    check("two_confirmed_converged_single_points", len(gas) == len(water) == 1,
          {"native_count": len(native), "eligible_gas": len(gas), "eligible_water": len(water)})
    difference = None
    if len(gas) == len(water) == 1:
        g, w = gas[0], water[0]
        gs, ws = g["parsed_input"], w["parsed_input"]
        check("same_non_solvent_scientific_input", gs["scientific_text_without_geometry_resources_or_solvent"] == ws["scientific_text_without_geometry_resources_or_solvent"],
              "Compares every simple keyword and remaining input body, excluding only geometry, resource controls and explicit CPCM settings; method suitability is separately reviewed.")
        check("neutral_singlet_both", gs["charge"] == ws["charge"] == 0 and gs["multiplicity"] == ws["multiplicity"] == 1, {})
        gc, wc = g["input_coordinates"], w["input_coordinates"]
        same_geometry = bool(gc and wc and exact_coordinates(gc, wc))
        check("same_ordered_exact_input_coordinates", same_geometry, {"gas_xyz": g["geometry_source"], "water_xyz": w["geometry_source"]})
        check("ethanol_atom_inventory", bool(gc and len(gc) == 9 and Counter(row[0] for row in gc) == {"C": 2, "H": 6, "O": 1}),
              "Atom inventory alone does not establish connectivity; prepared identity/provenance is retained separately.")
        shared = [p for p in preparation if gc and exact_coordinates(p["coordinates"], gc)]
        check("matches_preserved_prepared_geometry", bool(shared), {"prepared_artifact_ids": [p["source"]["artifact_id"] for p in shared]})
        gr, wr = g["geometry_source"], w["geometry_source"]
        byte_shared = [p for p in shared if gr and wr and
            p["source"]["sha256"] == gr["sha256"] == wr["sha256"] and
            p["source"]["bytes"] == gr["bytes"] == wr["bytes"]]
        check("same_preserved_prepared_xyz_bytes", bool(byte_shared),
              {"prepared_artifact_ids": [p["source"]["artifact_id"] for p in byte_shared],
               "gas_xyz_sha256": gr["sha256"] if gr else None,
               "water_xyz_sha256": wr["sha256"] if wr else None,
               "meaning": "Exact archived XYZ bytes, including comment and coordinate precision; independent of numerical coordinate equality."})
        gb, wb = g["raw_output"]["coordinate_blocks"], w["raw_output"]["coordinate_blocks"]
        raw_ordered = bool(len(gb) == len(wb) == 1 and gc and wc
            and exact_coordinates(gb[0]["rows"], wb[0]["rows"])
            and [row[0] for row in gb[0]["rows"]] == [row[0] for row in gc]
            and [row[0] for row in wb[0]["rows"]] == [row[0] for row in wc])
        check("same_ordered_raw_output_coordinates", raw_ordered,
              {"gas_coordinate_headers": [row["header_line"] for row in gb],
               "water_coordinate_headers": [row["header_line"] for row in wb],
               "meaning": "The two printed Angstrom coordinate blocks agree exactly with each other and retain input atom order. Printed output precision is not claimed to reproduce every input decimal."})
        ge = decimal(g["raw_output"]["energy_records"][0]["hartree"])
        we = decimal(w["raw_output"]["energy_records"][0]["hartree"])
        delta = we - ge
        difference = {"gas_energy_Eh": str(ge), "water_energy_Eh": str(we), "water_minus_gas_Eh": str(delta),
            "constant_kJ_mol_per_Eh": str(CONVERSION), "water_minus_gas_kJ_mol": str(delta * CONVERSION),
            "sign": "negative" if delta < 0 else "positive" if delta > 0 else "zero",
            "scope": "Specified-model electronic energy difference at the shared geometry; not hydration free energy.",
            "comparison_validity": "Only established if geometry/settings/solvent checks and manual review all pass; raw arithmetic alone is not validity."}

    selection_records = []
    first_science = min((row["created_at"] for row in native if row["created_at"]), default=None)
    for record in run["model_records"]:
        path = directory / "model" / f"{record['id']}.response.json"
        response = json.loads(path.read_bytes())
        assert evidence.retain(path)["sha256"] == record["response_record_sha256"]
        proposal = response.get("proposal") or {}
        if proposal.get("action") == "select_conditions":
            selection_records.append({"response": evidence.retain(path), "model_id": record["id"],
                "created_at": record.get("created_at"), "before_first_science": bool(first_science and record.get("created_at") and record["created_at"] < first_science),
                "choices": proposal["parameters"].get("choices"),
                "scope": "Public structured choice/reason fields only; provider reasoning is not transcribed."})
    reports = [evidence.retain(base / name) for name in ("report.json", "report.txt") if (base / name).is_file()]
    manual = {name: {"status": "unverified", "reason": "Read source-specific report/table and rationale before deciding."} for name in MANUAL_ITEMS}
    review_ref = None
    if args.review_json:
        review_ref = evidence.retain(args.review_json.resolve())
        reviewer = json.loads(args.review_json.read_bytes())
        assert reviewer["candidate"] == args.candidate and reviewer["run_id"] == run["id"]
        assert set(reviewer["checks"]) == set(MANUAL_ITEMS)
        assert all(value["status"] in {"pass", "fail", "unverified"} for value in reviewer["checks"].values())
        manual = reviewer["checks"]
    report = {"schema": "independent-t5-raw-check-3", "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "candidate": args.candidate, "run_id": run["id"], "scope": "Read-only original evidence and arithmetic; no independent solver or generated-code execution.",
        "prior_audit": evidence.retain(base / "independent-raw-check.json"),
        "audit_correction": "The v2 input parser did not accept CRLF before end-of-line anchors. This appended audit normalizes line endings only in its parsing view. Original input bytes/hashes, raw arithmetic, production state and manual failure remain unchanged; the prior audit is preserved.",
        "authorities": authorities, "requirements": task["fixed_requirements"], "tolerances": authority["common_tolerances"],
        "production_state": {"state": run["state"], "delivery_status": outcome["delivery_status"],
            "recorded_goal_status": outcome.get("goal_status"), "usage": run["usage"],
            "unknown_fields": outcome["unknown_fields"],
            "accounting_note": "Unknown model token usage remains occupied and is not inferred from raw scientific evidence.",
            "meaning": "This independent raw review never upgrades recorded Goal status or changes the historical Run."},
        "preparation": preparation, "native_attempts": native, "checks": checks, "independent_raw_difference": difference,
        "method_choice_records": selection_records, "analysis_programs": programs,
        "delivered_data_files": data_products, "product_reports": reports, "manual_review": manual,
        "manual_review_source": review_ref, "registered_source_files": evidence.files, "result_records": results,
        "raw_delivery_review_status": "pass" if checks and all(row["status"] == "pass" for row in checks)
            and all(value["status"] == "pass" for value in manual.values()) else "fail" if any(value["status"] == "fail" for value in manual.values()) else "unverified",
        "independent_same_settings_reference": "not_run; unverified",
        "formal_acceptance": "not_established; this development candidate is not the full same-version repetition/control/reference campaign",
        "scientific_qualification_assigned": False, "original_files_unchanged": evidence.unchanged(),
        "source_file_count_checked": len(evidence.hashes), "audit_script": evidence.retain(Path(__file__)),
        "native_discovery_script": evidence.retain(Path(__file__).with_name("native_raw_sources.py"))}
    assert report["original_files_unchanged"]
    payload = (json.dumps(report, ensure_ascii=False, separators=(",", ":"), allow_nan=False) + "\n").encode()
    assert len(payload) <= 262144, "Bounded audit exceeded file limit; refine references without truncating original evidence"
    if args.write:
        with destination.open("xb") as stream:
            stream.write(payload)
        assert evidence.unchanged()
    print(json.dumps({"candidate": args.candidate, "written": args.write, "destination": str(destination),
        "bytes": len(payload), "sha256": digest(payload), "native_attempts": len(native), "checks": checks,
        "raw_difference": difference, "manual_status": {k: v["status"] for k, v in manual.items()},
        "reports": reports, "analysis_programs": programs, "data_products": data_products,
        "original_files_unchanged": evidence.unchanged()}, ensure_ascii=False))


if __name__ == "__main__":
    main()
