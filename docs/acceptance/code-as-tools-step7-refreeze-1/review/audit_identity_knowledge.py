"""Independent local-byte audit; stdlib only, no product imports or network."""

import hashlib
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

REVIEW = Path(__file__).resolve().parent
PROJECT = REVIEW.parents[3]
BATCH = PROJECT / "data/code-tools-refreeze-1-batch"
FREEZE = REVIEW.parent / "freeze"


def read(path):
    raw = path.read_bytes()
    if len(raw) > 8 * 1024 * 1024:
        raise ValueError("review JSON exceeds 8 MiB bound")
    return json.loads(raw)


def digest(path):
    result = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(65536):
            result.update(chunk)
    return result.hexdigest()


def scope(path, root):
    path, root = path.resolve(), root.resolve()
    if not path.is_relative_to(root):
        raise ValueError("source path escapes task data root")
    return path


def check(issues, condition, label):
    if not condition:
        issues.append(label)


def artifact_record(root, run, call, result, identifier, issues):
    metadata_path = root / "artifacts" / identifier / "artifact.json"
    item = read(metadata_path)
    path = scope(root / item["path"], root)
    actual = digest(path)
    call_path = root / "runs" / run["id"] / "calls" / call["id"] / path.name
    call_hash = digest(call_path)
    check(issues, item["id"] == identifier and identifier in result["artifact_ids"], "artifact_id_binding")
    check(issues, item["run_id"] == run["id"], "artifact_run_binding")
    check(issues, item["source"].get("call_id") == call["id"], "artifact_call_binding")
    check(issues, item["source"].get("run_id") == run["id"], "artifact_source_run_binding")
    check(issues, actual == item["sha256"] == call_hash, "artifact_and_call_bytes_hash")
    check(issues, item["size"] == path.stat().st_size == call_path.stat().st_size, "artifact_size")
    return item, {
        "artifact_id": identifier, "role": item["role"], "filename": path.name,
        "path": str(path), "metadata_sha256": digest(metadata_path), "bytes": path.stat().st_size,
        "sha256": actual, "call_file": str(call_path), "call_file_sha256": call_hash,
    }


def identity_facts(root, result, artifacts):
    source = result["source"]
    requested = source.get("requested_identity", {}).get("requested_names", [])
    name = source.get("name_evidence", {}).get("name_for_evidence") or (requested[0] if requested else "")
    facts = {"requested_names": requested, "query_name": source.get("canonical_name"),
             "name_evidence_target": name, "http_requests_recorded": source.get("http_requests")}
    for item in artifacts:
        payload = read(Path(item["path"]))
        if "PropertyTable" in payload:
            facts["pubchem_candidates"] = [
                {key: row.get(key) for key in ("CID", "MolecularFormula", "SMILES", "Charge")}
                for row in payload["PropertyTable"].get("Properties", [])
            ]
        if "InformationList" in payload:
            facts["synonym_evidence"] = [
                {"cid": row.get("CID"), "synonym_count": len(row.get("Synonym", [])),
                 "exact_name_match": name.casefold() in [value.casefold() for value in row.get("Synonym", [])]}
                for row in payload["InformationList"].get("Information", [])
            ]
        if "search" in payload:
            facts["wikidata_search_ids"] = [row.get("id") for row in payload["search"]]
        if "entities" in payload:
            facts["wikidata_entities"] = []
            for identifier, entity in payload["entities"].items():
                labels = [row["value"] for row in entity.get("labels", {}).values()]
                labels += [row["value"] for values in entity.get("aliases", {}).values() for row in values]
                facts["wikidata_entities"].append({
                    "id": identifier, "exact_name_match": name.casefold() in [value.casefold() for value in labels],
                    "pubchem_cids": [row.get("mainsnak", {}).get("datavalue", {}).get("value")
                                    for row in entity.get("claims", {}).get("P662", [])],
                })
        if item["filename"] == "identity.json":
            facts["resolved_identity"] = {key: payload.get(key)
                for key in ("cid", "formula", "alias_verified", "name_evidence", "limitations")}
    return facts


def audit(task):
    start_path = BATCH / "first-repetition-evidence" / task / "start.json"
    start = read(start_path)
    root = scope(Path(start["data_root"]), BATCH)
    run_path = root / "runs" / start["run_id"] / "run.json"
    run_digest = digest(run_path)
    run = read(run_path)
    if run["state"] not in {"failed", "completed", "cancelled", "timed_out", "stopped", "budget_exhausted"}:
        raise ValueError("review only settled tasks")
    frozen = read(FREEZE / "product-freeze.json")["contract"]["knowledge"]
    documents, identities, issues = [], [], []
    calls = [call for call in run["calls"] if call["tool"] in {"knowledge.search", "structure.resolve"}]
    for call in calls:
        problems = []
        path = root / "runs" / run["id"] / "results" / (call["result_id"] + ".json")
        result = read(path)
        check(problems, result["id"] == call["result_id"] and result["id"] in run["result_ids"], "result_id_binding")
        check(problems, result["run_id"] == run["id"] and result["call_id"] == call["id"], "result_run_call_binding")
        check(problems, result["operation_status"] == call["state"], "call_result_terminal_status")
        check(problems, result["source"].get("tool") == call["tool"], "result_tool_binding")
        artifacts, metadata = [], {}
        for identifier in result["artifact_ids"]:
            item, record = artifact_record(root, run, call, result, identifier, problems)
            metadata[identifier] = item
            artifacts.append(record)
        row = {"call_id": call["id"], "result_id": result["id"], "result_sha256": digest(path),
               "operation_status": result["operation_status"], "artifacts": artifacts, "issues": problems}
        if call["tool"] == "knowledge.search":
            doc = result["observations"].get("document_excerpt", {})
            row.update({key: doc.get(key) for key in (
                "status", "document_id", "document_version", "url", "document_sha256",
                "excerpt_sha256", "document_artifact_id", "document_archive_complete", "document_bytes")})
            for key in ("index_version", "index_sha256"):
                check(problems, doc.get(key) == frozen[key], "frozen_" + key)
            identifier = doc.get("document_id", "")
            allowed_url = frozen["documents"].get(identifier, [None, None])[1]
            if identifier.startswith("orca:") and re.fullmatch(frozen["manual_reference"], identifier):
                allowed_url = frozen["manual_root"] + identifier[5:]
            check(problems, allowed_url == doc.get("url"), "frozen_document_url")
            check(problems, identifier == call["parameters"]["document"], "query_document_binding")
            check(problems, doc.get("query") == call["parameters"]["query"], "query_text_binding")
            excerpt_hash = hashlib.sha256(doc.get("excerpt", "").encode("utf-8")).hexdigest()
            check(problems, excerpt_hash == doc.get("excerpt_sha256"), "excerpt_hash")
            row["independent_excerpt_sha256"] = excerpt_hash
            if result["operation_status"] == "completed":
                aid = doc.get("document_artifact_id")
                check(problems, doc.get("document_archive_complete") is True and aid in metadata, "complete_archive_binding")
                if aid in metadata:
                    item = metadata[aid]
                    check(problems, item["role"] == "knowledge_document", "knowledge_role")
                    check(problems, item["sha256"] == doc.get("document_sha256"), "document_body_hash")
                    check(problems, item["size"] == doc.get("document_bytes"), "document_body_size")
                    for key in ("run_id", "call_id", "tool", "url", "document_id", "document_version", "accessed_at", "byte_semantics"):
                        check(problems, item["source"].get(key) == doc["source"].get(key) == result["source"].get(key),
                              "provenance_" + key)
                    name = Path(item["path"]).name
                    expected = {"artifact_id": aid, "sha256": item["sha256"]}
                    check(problems, doc["source"]["files"].get(name) == result["source"]["files"].get(name) == expected,
                          "result_file_binding")
            check(problems, not result["qualified_outputs"] and doc.get("scientific_qualification") is False,
                  "knowledge_not_scientific_qualification")
            documents.append(row)
        else:
            row["diagnostics"] = result["diagnostics"]
            row["qualified_ports"] = list(result["qualified_outputs"])
            row["facts_from_archived_identity_responses"] = identity_facts(root, result, artifacts)
            receipt_path = root / "runs" / run["id"] / "calls" / call["id"] / "receipt.json"
            receipt = read(receipt_path)
            calculated = hashlib.sha256(json.dumps(receipt["data"], sort_keys=True,
                separators=(",", ":"), allow_nan=False).encode("utf-8")).hexdigest()
            check(problems, calculated == receipt["data_sha256"], "identity_receipt_data_hash")
            check(problems, receipt["artifact_hashes"] == {key: item["sha256"] for key, item in metadata.items()},
                  "identity_receipt_artifact_hashes")
            expected_basis = {key: call[key] for key in ("id", "tool", "step_id", "request_version", "parameters", "consumption")}
            expected_basis["call_id"] = expected_basis.pop("id")
            expected_basis["run_id"] = run["id"]
            check(problems, receipt["basis"] == expected_basis, "identity_receipt_call_basis")
            check(problems, receipt["data"].get("diagnostics", []) == result["diagnostics"], "identity_receipt_diagnostics")
            check(problems, receipt["data"].get("artifact_ids", []) == result["artifact_ids"], "identity_receipt_artifact_ids")
            raw_outputs = receipt["data"].get("qualified_outputs", {})
            check(problems, set(raw_outputs) == set(result["qualified_outputs"]), "identity_receipt_qualified_ports")
            for port, value in raw_outputs.items():
                output = result["qualified_outputs"].get(port, {})
                check(problems, output.get("artifact_id") == value.get("artifact_id")
                      and output.get("source") == value.get("source"), "identity_qualified_artifact_source")
                expected_checks = value.get("checks", [])
                actual_checks = output.get("checks", [])
                check(problems, len(actual_checks) == len(expected_checks) and all(
                    all(actual.get(key) == child for key, child in expected.items())
                    for actual, expected in zip(actual_checks, expected_checks)), "identity_qualified_checks")
            for key, value in receipt["data"].get("source", {}).items():
                check(problems, result["source"].get(key) == value, "identity_receipt_source_" + key)
            source = result["source"]
            refs = ([{"artifact_id": source["raw_artifact_id"], "sha256": source.get("raw_sha256")}]
                    if source.get("raw_artifact_id") else [])
            refs += ([source["alias_evidence"]] if source.get("alias_evidence") else [])
            refs += source.get("cross_reference_evidence", [])
            for reference in refs:
                aid = reference.get("artifact_id")
                check(problems, aid in metadata and reference.get("sha256") == metadata[aid]["sha256"],
                      "identity_source_artifact_hash")
            if result["operation_status"] != "completed":
                check(problems, not result["qualified_outputs"], "failed_identity_publishes_no_qualified_output")
            row["receipt_sha256"] = digest(receipt_path)
            identities.append(row)
        issues.extend({"call_id": call["id"], "issue": problem} for problem in problems)
    check(issues, run_digest == digest(run_path), "run_changed_during_review")
    driver_path = BATCH / "first-repetition-evidence" / task / "knowledge-audit.json"
    driver = read(driver_path)
    check(issues, {row["result_id"] for row in driver["documents"]} == {row["result_id"] for row in documents},
          "driver_document_coverage")
    return {"schema_version": 1, "task": task, "run_id": run["id"], "run_state": run["state"],
            "reviewed_at_utc": datetime.now(timezone.utc).isoformat(), "reviewer": "/root/archive_knowledge",
            "method": "Independent stdlib local JSON and byte reads; no product imports, HTTP, model, or calculation.",
            "start_sha256": digest(start_path), "run_sha256": run_digest, "data_root": str(root),
            "driver_knowledge_audit_sha256": digest(driver_path), "knowledge_calls": len(documents),
            "identity_calls": len(identities), "knowledge_documents": documents, "identity_results": identities,
            "identity_failure_count": sum(row["operation_status"] != "completed" for row in identities),
            "issues": issues, "status": "bindings_verified" if not issues else "issues_found",
            "limitations": ["These checks establish retained byte/provenance consistency, not scientific correctness.",
                            "No new retrieval was made; external contents and missing identity evidence are not repaired."]}


if __name__ == "__main__":
    for task in sys.argv[1:]:
        if task not in {"T1", "T2", "T3", "T4", "T5", "H1", "H2"}:
            raise ValueError("unknown frozen task")
        record = audit(task)
        target = REVIEW / ("identity-knowledge-" + task.lower() + ".json")
        with target.open("x", encoding="utf-8", newline="\n") as stream:
            json.dump(record, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
        print(json.dumps({key: record[key] for key in (
            "task", "status", "knowledge_calls", "identity_calls", "identity_failure_count", "issues")}))
