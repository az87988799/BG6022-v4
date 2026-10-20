"""Audit settled H2 records. Never execute the archived task code."""

import importlib.util
import json
import re
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("holdout_audit", HERE / "holdout_readonly_audit.py")
audit = importlib.util.module_from_spec(spec)
spec.loader.exec_module(audit)
x = audit.snapshot("H2")
a, run, artifacts = x["audit"], x["run"], x["artifacts"]
rp = (audit.PROJECT / a["evidence_index"]["run"]["path"]).parent


def artifact(aid):
    return {"artifact_id": aid, **artifacts[aid]["body_file"]}


def decode_context(wire):
    """Expand only the declared key/pool/row syntax, without production imports."""
    keys, pool = wire["KEYS"], wire["SHARED_STRINGS"]

    def decode(value):
        if isinstance(value, str) and re.fullmatch(r"@[0-9]+", value):
            return pool[int(value[1:])]
        if isinstance(value, list):
            return [decode(row) for row in value]
        if isinstance(value, dict):
            if set(value) == {"@"}:
                return pool[value["@"]]
            result = {keys.get(key, key): decode(row) for key, row in value.items()}
            if "@rows" in result and "@columns" in result:
                return [dict(zip(result["@columns"], row)) for row in result["@rows"]]
            return result
        return value

    return decode({key: value for key, value in wire.items()
                   if key not in {"KEYS", "SHARED_STRINGS"}})


selection_id = "model_6adf1981b52342168504e93affccc26a"
request_path = rp / "model" / f"{selection_id}.request.json"
response_path = rp / "model" / f"{selection_id}.response.json"
request_body, response = audit.read(request_path), audit.read(response_path)
wire = json.loads(request_body["messages"][1]["content"])
expanded = decode_context(wire)
raw_proposal = json.loads(response["raw_content"])
assert raw_proposal == response["proposal"]
wrong_entry = raw_proposal["parameters"]["steps"][0]["parameters"]["inputs"]["entries"][0]
attempt = run["attempts"][0]
result = x["results"][attempt["result_id"]]
assert len(run["attempts"]) == 1 and attempt["tool"] == "python.execute"
assert wrong_entry == attempt["consumption"]["inputs"]["entries"][0]
assert wrong_entry == attempt["frozen_step"]["parameters"]["inputs"]["entries"][0]
assert wrong_entry["sha256"] == artifacts[wrong_entry["artifact_id"]]["body_file"]["sha256"]
assert result["source"]["code_sha256"] == attempt["consumption"]["code"]["sha256"]
assert artifacts[attempt["consumption"]["code"]["artifact_id"]]["body_file"]["sha256"] == \
    attempt["consumption"]["code"]["sha256"]
facts = expanded["DATA"]["delivery"]["facts"]
prepared_fact = facts[4]["value"]
assert prepared_fact["artifact_id"] == "artifact_b6d9dee91f83454998ee1017bcceafb0"
assert prepared_fact["kind"] == "qualified_input_artifact"
window = expanded["DATA"]["results"]["rows"][6][7]["text_window"]
assert window["artifact_id"] == prepared_fact["artifact_id"]
assert window["sha256"] == artifacts[prepared_fact["artifact_id"]]["body_file"]["sha256"]
assert window["coverage"]["complete"] and window["lines"][0]["text"] == "4"
assert a["all_artifact_bytes_hashes_and_run_match"]
assert all(row["request_hash_matches"] and row["response_record_hash_matches"]
           and row["original_full_task_visible"] for row in a["model_receipts"])
assert all(row["bound"] and row["archive_complete"] for row in a["knowledge_archive_bindings"])
assert all(row["matches"] for row in a["result_source_bindings"])
assert a["model_receipt_totals"]["total_tokens"] == run["usage"]["model_tokens_used"]
assert run["usage"]["orca_starts_actual"] == 0 and not run["internal_calls"]

selection_audit = {
    "request": audit.ref(request_path),
    "response": audit.ref(response_path),
    "actual_raw_proposal_input_entry": wrong_entry,
    "raw_response_equals_parsed_proposal": True,
    "raw_proposal_equals_frozen_attempt_input_entry": True,
    "id_was_full_literal_not_a_short_alias": True,
    "correct_prepared_geometry_visible": {
        "decoded_path": "DATA.delivery.facts[4].value",
        "value": prepared_fact,
        "raw_text_window_decoded_path": "DATA.results.rows[6][7].text_window",
        "raw_text_window": window,
    },
    "wrong_artifact_visible_as": {
        "decoded_path": "DATA.delivery.facts[3].value.prepared_geometry[0].source",
        "identity_artifact_id": facts[3]["value"]["prepared_geometry"][0]["source"]["identity_artifact_id"],
        "identity_sha256": facts[3]["value"]["prepared_geometry"][0]["source"]["identity_sha256"],
    },
    "selected_identity_bytes": artifact("artifact_1fccd49ea14b4809820424f134f0c382"),
    "actual_task_copy_named_xyz": artifact("artifact_43ff07dd20da47b0bb2a0625516e5a8c"),
    "prepared_geometry_not_selected": artifact("artifact_b6d9dee91f83454998ee1017bcceafb0"),
    "classification": "The raw model proposal selected the wrong existing Artifact. The program "
                      "copied exactly its declared bytes; no alias-decoding substitution was found.",
}
selection_target = HERE / "H2-input-selection-audit.json"
with selection_target.open("x", encoding="utf-8") as stream:
    json.dump(selection_audit, stream, ensure_ascii=False, indent=2)
    stream.write("\n")

evidence_reads = []
for row in run["model_records"]:
    proposal = audit.read(rp / "model" / f"{row['id']}.response.json").get("proposal") or {}
    if proposal.get("action") in {"evidence.text", "evidence.list"}:
        evidence_reads.append({"model_id": row["id"], "action": proposal["action"],
                               "parameters": proposal["parameters"], "reason": proposal["reason"]})

stderr_id = "artifact_b949fba5cae8433b8f403e6e28a00039"
stderr_results = []
for result_id, row in x["results"].items():
    text_window = row.get("observations", {}).get("text_window", {})
    if text_window.get("artifact_id") == stderr_id:
        assert len(text_window["lines"]) == 10
        assert "ValueError: invalid literal" in text_window["lines"][-1]["text"]
        assert text_window["coverage"]["complete"] and not text_window["coverage"]["truncated"]
        stderr_results.append({"result": audit.ref(rp / "results" / f"{result_id}.json"),
                               "text_window": text_window})
assert len(stderr_results) == 4
preview_requests = []
for model_id in ("model_47c0e8c9514d417793693694a97aea0a",
                 "model_cc8f5e6570d44d6b9fc7bc861e68d1c8"):
    path = rp / "model" / f"{model_id}.request.json"
    raw = audit.read(path)["messages"][1]["content"]
    context = json.loads(raw)
    pages = [{"pool_index": i, "value": value}
             for i, value in enumerate(context["SHARED_STRINGS"])
             if isinstance(value, dict) and value.get("next_line") == 7]
    assert pages and all(p["value"]["shown"] == 6 and
                         p["value"]["omitted_from_result"] == 4 for p in pages)
    assert "ValueError" not in raw and "invalid literal" not in raw
    preview_requests.append({"request": audit.ref(path),
                             "exception_tail_present_in_actual_request": False,
                             "explicit_context_page_metadata": pages})
stderr_read_proposals = [row for row in evidence_reads
                        if row["parameters"].get("artifact_id") == stderr_id]
assert len(stderr_read_proposals) == 4
assert all(row["parameters"]["start_line"] == 1 for row in stderr_read_proposals)
feedback = {
    "schema": "independent-failure-feedback-audit-1", "task": "H2",
    "finding": "The tool archived the full ten-line traceback. Model requests provided a six-line "
               "preview with an explicit next_line=7 hint; the model repeatedly requested from line "
               "1 and never requested that tail. One intervening proposal read empty stdout instead. "
               "Thus the selected raw requests did not contain the actual ValueError tail, even "
               "though tool Results did; this is not evidence of a failed tool read.",
    "stderr_artifact": artifact(stderr_id), "full_tool_results": stderr_results,
    "actual_request_previews": preview_requests,
    "repeated_stderr_read_proposals": stderr_read_proposals,
    "wrong_stdout_read_response":
        audit.ref(rp / "model/model_535e1cf01b544e6eaa68c549b5a971a6.response.json"),
    "unknown_artifact_rejection_response":
        audit.ref(rp / "model/model_3584689e310d48c4a502d13f465263ba.response.json"),
    "no_repair_or_second_attempt": len(run["attempts"]) == 1,
}
feedback_path = HERE / "H2-feedback-audit.json"
with feedback_path.open("x", encoding="utf-8") as stream:
    json.dump(feedback, stream, ensure_ascii=False, indent=2)
    stream.write("\n")

review = {
    "schema": "independent-terminal-holdout-review-1",
    "task": "H2",
    "reviewed_at_utc": datetime.now(timezone.utc).isoformat(),
    "run_id": run["id"],
    "frozen_product_commit": "7cc496271909966de41fbadd15d8e24c095f97e1",
    "frozen_assets_commit": "323ac94",
    "scope": "One formal repetition of the previously exposed H2 task; not a fresh holdout evaluation.",
    "reviewer_actions": ["read existing local records and raw files", "sha256 existing bytes",
                         "decode declared context serialization", "compare input references",
                         "sum already reported usage"],
    "not_performed": ["model or API call", "HTTP retrieval", "ORCA or Python task execution",
                      "scientific recomputation", "retry", "reference or control execution",
                      "production, test, configuration or frozen-asset edit"],
    "registration": {name: audit.ref(HERE / name) for name in (
        "H2-requirement-registration.json", "H2-input-registration.json")},
    "conclusion": {
        "operation": "one Python Attempt executed and failed while parsing the first input line",
        "scientific_output": "unverified; zero ORCA starts and no polarizability tensors",
        "user_goal_complete": False, "delivery_status": "partial",
        "satisfied_required_goals": ["goal_prepare_ch2o"],
        "unmet_required_goals": [g["id"] for g in x["request"]["goals"]
                                if g["required"] and run["goal_status"][g["id"]] != "satisfied"],
        "reference_comparison": "not performed in this authorized round",
    },
    "original_and_projection": {
        "full_frozen_task_visible_in_all_24_raw_model_requests": True,
        "final_request_version": x["request"]["version"],
        "goal_ids": [g["id"] for g in x["request"]["goals"]],
        "structured_conditions": x["request"]["conditions"],
        "assessment": "No original task-clause removal found in the archived requests. Fixed geometry, "
                      "rotation and same-settings obligations remain quoted/shared constraints. "
                      "Text availability does not establish execution or scientific qualification.",
        "shared_constraints_by_goal": {g["id"]: g["text_evidence"].get("shared_constraints", [])
                                       for g in x["request"]["goals"]},
        "coverage": [
            "CH2O neutral singlet gas-phase identity and RHF/def2-SVP retained",
            "Fixed geometry, no separate optimization, original coordinate origin, axis (1,2,3), "
            "right-hand active +37 degrees and unchanged atom order/internal geometry retained",
            "Two independent real static dipole-dipole calculations with the same settings retained",
            "Two 3x3 tensors, isotropic values, atomic units, >=7 tensor decimals and >=5 isotropic "
            "decimals, covariance and isotropic-invariance verification retained",
            "Two XYZs, source/input/output/scripts/tables, no dynamic/dipole/hyperpolarizability "
            "substitution and no algebraically fabricated second result retained",
            "Seed 1701 or unsupported-seed disclosure is a required Goal and remains incomplete",
        ],
    },
    "identity_geometry": {
        "identity_result_id": "result_d11a9df079e8477fb441cae409550722",
        "prepare_result_id": "result_e9a37c695d324dafa50dccaf14248954",
        "contract": "general-identity-3 / structure-identity-3 / structure-prepare-3",
        "original_name": "甲醛CH2O", "query_name": "formaldehyde",
        "query_name_source": "inferred_translation_or_alias",
        "name_mapping_independently_verified": False,
        "prepared_atom_order": ["C", "O", "H", "H"],
        "prepared_geometry": artifact("artifact_b6d9dee91f83454998ee1017bcceafb0"),
        "geometry_scope": "Initial graph/atom mapping and finite coordinates only; not optimized or "
                          "ground-state certified. No seed control exposed by the generator.",
    },
    "authored_vs_executed": {
        "initial_script_registered_before_any_attempt":
            artifact("artifact_98246124ed4247dbbd32c0fc01c4fd94"),
        "initial_script_scope": "Proposed two SDK ORCA jobs and tensor comparison; not executed.",
        "actual_executed_script": artifact("artifact_2fe5ad55c2794b519064420aa8b6f392"),
        "actual_executed_scope": "Separate rotation-only Python Step with sdk_orca=false.",
        "actual_script_rotation": "R = I + sin(theta)K + (1-cos(theta))K^2; row coordinates Y=X@R.T; "
                                  "axis normalized (1,2,3), theta=37 degrees, original origin. "
                                  "Consistent with preregistered active column-vector convention.",
        "rotation_execution_status": "Failed before parsing atoms or writing rotated XYZ/rotation record.",
        "actual_rotated_coordinates_or_property_values_available": False,
        "source_only_seed_caveat": "The later unexecuted record-writing line says no random numbers "
                                  "used in geometry generation or rotation, whereas the initial "
                                  "preparation receipt only says no seed exposed. No rotation record "
                                  "was produced; do not treat this authored statement as established.",
    },
    "attempt": {
        "attempt_id": attempt["id"], "step_id": attempt["step_id"],
        "logical_id": attempt["logical_id"], "started": attempt["started"],
        "created_at": attempt["created_at"], "finished_at": attempt["finished_at"],
        "operation_status": result["operation_status"],
        "exit_code": result["source"]["execution"]["exit_code"],
        "evidence_collection_complete": result["observations"]["evidence_collection_complete"],
        "qualified_scientific_outputs": list(result["qualified_outputs"]),
        "sdk_orca": attempt["frozen_step"]["parameters"]["sdk_orca"],
        "code_and_input_consumption_hashes_match": True,
        "stderr": artifact("artifact_b949fba5cae8433b8f403e6e28a00039"),
        "exception": "ValueError: invalid literal for int() with base 10: '{'",
        "failing_task_line": 29,
        "failing_operation": "n = int(lines[0].split()[0])",
    },
    "input_selection_audit": audit.ref(selection_target),
    "causal_findings": [
        {
            "classification": "confirmed_model_artifact_selection_error",
            "fact": "The raw model response chose identity.json as ch2o_original.xyz while its purpose "
                    "claimed a prepared geometry. Exact ID/hash survives proposal, frozen Step, "
                    "Attempt and task copy. The first JSON line '{' is not an XYZ atom count.",
            "correct_geometry_visible_at_selection_time": True,
            "alias_decoding_corruption_found": False,
            "wrong_bytes_corruption_found": False,
        },
        {
            "classification": "confirmed_python_failure_before_scientific_execution",
            "fact": "Python started under the frozen container boundary and exited 1 at the first "
                    "geometry parse. It did not invoke the SDK or ORCA. The observed traceback "
                    "attributes failure to data selection/format, not isolation, resource exhaustion "
                    "or ORCA API/native-input failure.",
        },
        {
            "classification": "confirmed_no_repair_execution",
            "fact": "No second Python Attempt, corrected input manifest or subsequent ORCA execution "
                    "exists in the settled Run. Reading evidence and authoring earlier input files "
                    "did not complete a repair.",
        },
        {
            "classification": "confirmed_bounded_preview_and_failed_paging_loop",
            "fact": "Four stderr tool Results contain the full 10-line traceback. Actual model "
                    "requests show the first six lines with explicit next_line=7 / omitted=4 "
                    "context-page metadata; the ValueError tail is absent from those requests. "
                    "The model repeatedly requested from start_line=1 rather than page 7, and "
                    "also read empty stdout once. No repair followed. This combines a preview "
                    "limitation with unsuccessful model navigation; it is not a missing tool Result.",
            "audit": audit.ref(feedback_path),
        },
        {
            "classification": "confirmed_model_call_limit",
            "fact": "All 24 model calls were consumed; the last request was terminal-only. Scientific "
                    "ORCA allowance remained unused. File writes were 6 of 32; workspace.write did "
                    "not exhaust the analysis_executions budget.",
            "raw_final_request": audit.ref(rp / "model/model_a60aa4afad0d4d049e6a097bbf5ef041.request.json"),
        },
    ],
    "read_sequence": evidence_reads,
    "secondary_reporting_findings": [
        {
            "fact": "The final report says failed/partial and does not claim polarizability success. "
                    "Some carried Plan gaps and the final model reason still say prepared geometry "
                    "is missing, although goal_prepare_ch2o is satisfied and its Artifact is archived.",
            "delivery": audit.ref(rp / "deliveries/model_a60aa4afad0d4d049e6a097bbf5ef041.md"),
        },
        {
            "fact": "One later read proposal selected stdout while saying stderr; another referenced "
                    "an Artifact outside the permission snapshot and was rejected. These do not "
                    "replace the original Python failure or establish a repaired input.",
            "model_ids": ["model_535e1cf01b544e6eaa68c549b5a971a6",
                          "model_3584689e310d48c4a502d13f465263ba"],
        },
    ],
    "scientific_checks": {
        "initial_geometry": "qualified input, not optimized or ground-state certified",
        "specified_rotation": "authored convention matches; no generated rotated geometry to verify",
        "two_independent_real_orca_jobs": "not completed; zero actual starts",
        "tensor_components_and_isotropic_values": "not available",
        "raw_to_table_precision_and_units": "not evaluable",
        "trace_divided_by_three": "not evaluable",
        "rotation_covariance_and_isotropic_invariance": "not evaluable",
        "independent_reference": "not run",
        "reviewer_posterior_numerical_calculation": "none; there are no scientific values",
    },
    "resource_limitations": [
        "Cgroup elapsed time and CPU are sampled lower bounds; resource_usage_complete=false.",
        "A Python Attempt is recorded separately from the non-execution analysis_executions call counter; "
        "analysis_executions=0 does not mean no Python ran.",
        "Artifact size/count accounting is not a per-Attempt disk hard quota.",
        "Known model cost sums archived usage pricing records; no new invoice verification was made.",
    ],
    "integrity_and_usage": a,
    "review_code": {"audit_helper": audit.ref(HERE / "holdout_readonly_audit.py"),
                    "writer": audit.ref(Path(__file__))},
}
review["failure_feedback_audit"] = audit.ref(feedback_path)
target = HERE / "H2-review.json"
with target.open("x", encoding="utf-8") as stream:
    json.dump(review, stream, ensure_ascii=False, indent=2)
    stream.write("\n")
print(json.dumps({"file": str(target), "sha256": audit.sha(target),
                  "selection_audit_sha256": audit.sha(selection_target),
                  "feedback_audit_sha256": audit.sha(feedback_path),
                  "known_calls": a["model_receipt_totals"]["calls"],
                  "tokens": a["model_receipt_totals"]["total_tokens"],
                  "cost_known_usd": a["model_receipt_totals"]["known_cost_usd"]}))
