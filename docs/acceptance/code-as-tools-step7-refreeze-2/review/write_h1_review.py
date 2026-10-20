"""Build an independent terminal review from existing H1 evidence only."""

import importlib.util
import json
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("holdout_audit", HERE / "holdout_readonly_audit.py")
audit = importlib.util.module_from_spec(spec)
spec.loader.exec_module(audit)
x = audit.snapshot("H1")
a, run, artifacts = x["audit"], x["run"], x["artifacts"]
rp = (audit.PROJECT / a["evidence_index"]["run"]["path"]).parent


def artifact(aid):
    return {"artifact_id": aid, **artifacts[aid]["body_file"]}


attempts = []
for attempt in run["attempts"]:
    result = x["results"][attempt["result_id"]]
    code = attempt["consumption"]["code"]
    inputs = attempt["consumption"]["inputs"]["entries"]
    assert artifacts[code["artifact_id"]]["body_file"]["sha256"] == code["sha256"]
    assert result["source"]["code_sha256"] == code["sha256"]
    assert all(artifacts[i["artifact_id"]]["body_file"]["sha256"] == i["sha256"] for i in inputs)
    attempts.append({
        "attempt_id": attempt["id"], "step_id": attempt["step_id"],
        "logical_id": attempt["logical_id"], "created_at": attempt["created_at"],
        "finished_at": attempt["finished_at"], "state": attempt["state"],
        "started": attempt["started"], "result_id": result["id"],
        "code": artifact(code["artifact_id"]), "inputs": inputs,
        "consumption_and_result_code_hashes_match": True,
        "not_started": result["source"]["execution"]["not_started"],
        "exit_code": result["source"]["execution"]["exit_code"],
        "reason": result["source"]["execution"]["reason"],
        "qualified_scientific_outputs": list(result["qualified_outputs"]),
        "evidence_collection_complete": result["observations"]["evidence_collection_complete"],
    })

assert a["all_artifact_bytes_hashes_and_run_match"]
assert all(row["request_hash_matches"] and row["response_record_hash_matches"]
           and row["original_full_task_visible"] for row in a["model_receipts"])
assert all(row["bound"] and row["archive_complete"] for row in a["knowledge_archive_bindings"])
assert all(row["matches"] for row in a["result_source_bindings"])
assert a["model_receipt_totals"]["total_tokens"] == run["usage"]["model_tokens_used"]
assert len(attempts) == 2 and sum(row["started"] for row in attempts) == 1

review = {
    "schema": "independent-terminal-holdout-review-1",
    "task": "H1",
    "reviewed_at_utc": datetime.now(timezone.utc).isoformat(),
    "run_id": run["id"],
    "frozen_product_commit": "7cc496271909966de41fbadd15d8e24c095f97e1",
    "frozen_assets_commit": "323ac94",
    "scope": "One formal repetition of the previously exposed H1 task; not a fresh holdout evaluation.",
    "reviewer_actions": ["read existing local records and raw files", "sha256 existing bytes",
                         "sum already reported usage and compare exact bindings"],
    "not_performed": ["model or API call", "HTTP retrieval", "ORCA or Python task execution",
                      "scientific recomputation", "retry", "reference or control execution",
                      "production, test, configuration or frozen-asset edit"],
    "registration": {name: audit.ref(HERE / name) for name in (
        "H1-requirement-registration.json", "H1-method-registration.json",
        "H1-input-registration.json")},
    "conclusion": {
        "operation": "failed; one actual ORCA launch and one separate prelaunch rejection",
        "scientific_output": "unverified; no total GIAO tensors or isotropic shielding values",
        "user_goal_complete": False,
        "delivery_status": "partial",
        "satisfied_required_goals": ["goal_prepare_hcn_geometry"],
        "unmet_required_goals": [g["id"] for g in x["request"]["goals"]
                                if g["required"] and run["goal_status"][g["id"]] != "satisfied"],
        "reference_comparison": "not performed in this authorized round",
    },
    "original_and_projection": {
        "full_frozen_task_visible_in_all_24_raw_model_requests": True,
        "final_request_version": x["request"]["version"],
        "goal_ids": [g["id"] for g in x["request"]["goals"]],
        "assessment": "No source-text loss found: all original scientific/reporting clauses remain "
                      "in raw requests, quoted Goal text and the final delivery. Presence does not "
                      "prove model compliance or automatic scientific validation.",
        "structured_conditions": x["request"]["conditions"],
        "fixed_geometry_goals": [g["id"] for g in x["request"]["goals"]
                                if g["conditions"].get("geometry_relation") == "fixed_initial"],
        "minimum_evidence": {g["id"]: g["minimum_evidence"] for g in x["request"]["goals"]},
        "coverage": [
            "HCN identity, neutral singlet and gas phase retained",
            "Method choice restricted to RHF/PBE0 and basis def2-SVP/def2-TZVP in original text; "
            "approved PBE0/def2-TZVP selected before any Attempt",
            "Three nuclei, actual atom order, full 3x3 total GIAO tensor, isotropic ppm, coordinate "
            "frame, >=3 decimals and trace/3 requirement retained",
            "Initial/calculation geometries, raw files, scripts, tables and limitations retained",
            "No chemical-shift substitution, no dia/paramagnetic-only substitute and no missing "
            "value fabrication clauses retained",
            "Seed 1701 or unsupported-seed disclosure remains visible, but normalized Goal "
            "goal_random_seed_record is marked optional; the requirement was not completed",
        ],
    },
    "identity_geometry": {
        "identity_result_id": "result_bd43832e54b44d438269031b0b71dd34",
        "prepare_result_id": "result_ea4401f785474da0a5264c7a8ec08cf8",
        "contract": "general-identity-3 / structure-identity-3 / structure-prepare-3",
        "original_name": "氰化氢HCN", "query_name": "hydrogen cyanide",
        "query_name_source": "inferred_translation_or_alias",
        "name_mapping_independently_verified": False,
        "cid": 768, "smiles": "C#N", "formula": "CHN", "charge": 0,
        "prepared_atom_order": ["C", "N", "H"],
        "prepared_geometry": artifact("artifact_20c5a2c764b446dab4b3336b8a408510"),
        "actual_native_input_geometry_copy": artifact("artifact_d4780ddfe8f54fe3891e8107ed29f346"),
        "geometry_copy_exact_hash_match": True,
        "geometry_scope": "Initial graph/atom mapping and finite coordinates, not optimized or "
                          "ground-state certified. No seed exposed by generation; immutable XYZ reused.",
    },
    "attempts": attempts,
    "causal_findings": [
        {
            "classification": "confirmed_prelaunch_rejection",
            "attempt_id": "attempt_6626ed8e83404fc585da9c7b55895015",
            "fact": "Initial input omitted an explicit RKS/RHF/UHF/UKS wavefunction keyword. "
                    "The product returned 'one explicit wavefunction keyword is required; "
                    "no spin-state default is inferred' with not_started=true.",
            "actual_orca_starts": 0,
        },
        {
            "classification": "confirmed_native_input_parser_failure",
            "attempt_id": "attempt_b044bb82814f4a7987792ceecc157e56",
            "fact": "The next input added RKS and actually launched ORCA. ORCA exited 55 because "
                    "the EPRNMR block requested nuclear properties before coordinates were read.",
            "stderr": artifact("artifact_7ab95d9dfc774c4f967d39d4c0b949ba"),
            "raw_error": "Error in [EPRNMR] block: nuclear properties are requested but no coordinates "
                         "have been read! Please put the coordinates input before the [EPRNMR] block "
                         "in your input file!",
            "actual_orca_starts": 1,
            "not_evidence_of": ["SCF nonconvergence", "successful GIAO response", "resource exhaustion"],
        },
        {
            "classification": "confirmed_unexecuted_candidate",
            "fact": "A later writer moved the XYZ directive before EPRNMR. Its Artifact exists, "
                    "but no third Attempt or actual launch exists. Validity and success remain untested.",
            "candidate": artifact("artifact_32b9e9bb65894527a697d6016b48aba5"),
        },
        {
            "classification": "confirmed_model_call_limit",
            "fact": "The final raw request was terminal-only with allowed_actions=['stop'] and "
                    "model_calls_after_this_request=0. All 24 model calls were consumed; ORCA "
                    "remaining allowance was 10. The model's final stop did not execute the last candidate.",
            "request": audit.ref(rp / "model/model_3dafb327d6394d47aecc53e902a38e9f.request.json"),
        },
    ],
    "secondary_reporting_findings": [
        {
            "fact": "The method note, written before geometry preparation, says H-C-N atom order. "
                    "The qualified and actually copied XYZ order is C,N,H. No tensor table exists, "
                    "so this is stale prose, not proven numerical reindexing.",
            "source": artifact("artifact_879aafa6c1e449578ad4dc4eb6b74de4"),
        },
        {
            "fact": "Later model reasons conflate the two earlier Attempts as EPRNMR failures, "
                    "although the first did not start ORCA. The review separates them.",
            "response_ids": ["model_4714795aea6c449fa02e0c7b116cfde5",
                             "model_820781760fd947a097e3c19943a9ca1a"],
        },
        {
            "fact": "The penultimate model called evidence.text on method_notes.md while its reason "
                    "described inspecting the candidate ORCA input.",
            "response": audit.ref(rp / "model/model_46d6b18a007246c98195fe64c6aca4b4.response.json"),
        },
        {
            "fact": "The generated report leads with failed/partial and does not claim shielding "
                    "success. Its stop diagnosis still shows the earlier corrected method-source "
                    "hash proposal rejection; that stale label is not the final ORCA failure mechanism.",
            "delivery": audit.ref(rp / "deliveries/model_3dafb327d6394d47aecc53e902a38e9f.md"),
        },
        {
            "fact": "The final delivery still says the method notes are being prepared and numerical "
                    "settings await input, although the notes and three input versions exist. These "
                    "Plan-gap descriptions are stale, not a factual inventory of missing files.",
            "delivery_line_numbers": [27, 63],
        },
        {
            "fact": "The final delivery line 40 correctly says the prepared geometry is checked input "
                    "without scientific qualification, but its generic Result output row at line 104 "
                    "labels prepared_geometry scientific qualification True. This terminology conflict "
                    "does not establish any NMR scientific output.",
            "delivery_line_numbers": [40, 104],
        },
        {
            "fact": "The method note says no random-number method is required for current geometry "
                    "preparation; the actual preparation source instead reports random_seed=null and "
                    "no exposed seed. The user-required seed/unsupported-seed explanation is not "
                    "resolved by the optional seed Goal's final insufficient-evidence status.",
            "source": artifact("artifact_879aafa6c1e449578ad4dc4eb6b74de4"),
        },
    ],
    "scientific_checks": {
        "raw_total_tensors_available": False, "raw_isotropic_values_available": False,
        "trace_divided_by_three": "not evaluable", "ppm_and_table_precision": "not evaluable",
        "all_three_nuclei": "not completed", "independent_reference": "not run",
        "fixed_input_geometry": "exact artifact hash preserved",
        "input_evidence_pass_is_not_shielding_validation": True,
    },
    "resource_limitations": [
        "Cgroup sampled runtime and CPU are lower bounds; resource_usage_complete=false.",
        "Artifact size/count accounting is not a per-Attempt disk hard quota.",
        "Known model cost is the sum of archived provider usage pricing records, not a new invoice check.",
        "Two native Attempt reservations remain charged, including the one that never launched ORCA.",
    ],
    "integrity_and_usage": a,
    "review_code": {
        "audit_helper": audit.ref(HERE / "holdout_readonly_audit.py"),
        "writer": audit.ref(Path(__file__)),
    },
}
target = HERE / "H1-review.json"
with target.open("x", encoding="utf-8") as stream:
    json.dump(review, stream, ensure_ascii=False, indent=2)
    stream.write("\n")
print(json.dumps({"file": str(target), "sha256": audit.sha(target),
                  "known_calls": a["model_receipt_totals"]["calls"],
                  "tokens": a["model_receipt_totals"]["total_tokens"],
                  "cost_known_usd": a["model_receipt_totals"]["known_cost_usd"]}))
