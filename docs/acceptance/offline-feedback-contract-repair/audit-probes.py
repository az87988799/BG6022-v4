"""Read-only independent accounting of the four developer Runs."""

import hashlib
import json
import math
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[3]
OUT = Path(__file__).resolve().parent
SOURCE = ROOT / "data/offline-feedback-contract-repair-validation"


def artifact(case, identity):
    folder = case / "data/artifacts" / identity
    metadata = json.loads((folder / "artifact.json").read_bytes())
    path, = list((folder / "files").iterdir())
    raw = path.read_bytes()
    assert hashlib.sha256(raw).hexdigest() == metadata["sha256"]
    return raw


def task_json(case, result, name):
    reference = result["source"]["files"]["task/" + name]
    return json.loads(artifact(case, reference["artifact_id"]))


def main():
    target = OUT / "real-orca-audit.json"
    assert not target.exists()
    records, stdout_records, cpu, wall = [], [], 0.0, 0.0
    for case in sorted(SOURCE.iterdir()):
        if not case.is_dir() or not (case / "probe-summary.json").is_file():
            continue
        data = json.loads((case / "evidence.json").read_bytes())
        run, result = data["run"], data["result"]
        assert data["outcome"]["state"] == "completed"
        assert run["configuration"]["model_profile"] == "disabled"
        assert run["permission"]["model_execution"] is False
        assert run["budget"]["model_calls"] == run["usage"]["model_calls"] == 0
        assert not run["model_records"] and not result["qualified_outputs"]
        assert all(a["state"] in {"failed", "completed"} and a["finished_at"]
                   for a in run["attempts"])
        assert all(c["state"] == "completed" and c["finished_at"] for c in run["internal_calls"])
        usage = run["usage"]
        assert usage["orca_starts_reserved"] <= run["budget"]["orca_starts"]
        cpu += usage["cpu_seconds"]
        wall += usage["elapsed_seconds"]
        details = {}
        if case.name.startswith("test_tddft"):
            parsed = task_json(case, result, "parsed.json")
            assert len(parsed["rows"]) == 5
            details["transition_rows"] = parsed["rows"]
        elif case.name.startswith("test_two"):
            parsed = task_json(case, result, "rotation.json")
            first, second = map(np.array, parsed["tensors"])
            eigen_error = float(np.max(np.abs(np.linalg.eigvalsh(first) - np.linalg.eigvalsh(second))))
            isotropic_error = float(abs(np.trace(first) - np.trace(second)) / 3)
            assert eigen_error < 1e-3 and isotropic_error < 1e-4
            details = {"eigenvalue_difference_max_au": eigen_error,
                       "isotropic_difference_au": isotropic_error,
                       "covariance_error_au": parsed["max_abs_covariance_error_au"]}
        elif case.name.startswith("test_conformer"):
            parsed = task_json(case, result, "conformers.json")
            assert parsed["generated"] == 6 and parsed["computed"] == 2
            assert parsed["excluded_by_scope"] == 4
            energies = [row["energy_hartree"] for row in parsed["rows"]]
            assert len(energies) == 2 and all(math.isfinite(e) for e in energies)
            details = {"generated": 6, "computed": 2, "excluded_by_scope": 4,
                       "energies_hartree": energies}
        else:
            original = json.loads((OUT / "evidence/initial" / case.name / "evidence.json").read_bytes())
            assert run["id"] == original["run"]["id"]
            assert run["attempts"][0] == original["run"]["attempts"][0]
            assert run["attempts"][1]["logical_id"] == run["attempts"][0]["logical_id"]
            assert usage["orca_starts_actual"] == 1 and usage["orca_starts_reserved"] == 2
            details = {"initial_attempt_unchanged": True, "initial_actual_start_confirmed": False,
                       "same_run_and_logical_step": True, "shielding_tensors": 3}
        for attempt in run["attempts"]:
            stored = json.loads((case / "data/runs" / run["id"] / "results" /
                                 (attempt["result_id"] + ".json")).read_bytes())
            for key, ref in stored["source"].get("files", {}).items():
                folder = case / "data/artifacts" / ref["artifact_id"] / "files"
                payload, = list(folder.iterdir())
                if payload.name != "stdout.out":
                    continue
                raw = artifact(case, ref["artifact_id"])
                if b"Your ORCA version" in raw or b"O   R   C   A" in raw:
                    stdout_records.append({"run_id": run["id"], "attempt_id": attempt["id"],
                                           "file": key, "sha256": hashlib.sha256(raw).hexdigest(),
                                           "normal_termination": b"ORCA TERMINATED NORMALLY" in raw})
        records.append({"case": case.name, "run_id": run["id"], "usage": usage, "details": details})
    assert len(records) == 4
    assert sum(r["usage"]["orca_starts_reserved"] for r in records) == 7
    assert sum(r["usage"]["orca_starts_actual"] for r in records) == 6
    assert len(stdout_records) == 7
    assert sum(item["normal_termination"] for item in stdout_records) == 6
    environment = Path(json.loads((ROOT / "docs/acceptance/code-as-tools-step7-refreeze-1/freeze/"
                                   "product-freeze-event.json").read_bytes())["environment_root"])
    assert not (environment / "lease.json").exists()
    audit = {"developer_authored": True, "formal_slots_used": 0, "real_model_calls": 0,
             "orca_reservations": 7, "process_confirmed_orca_starts": 6,
             "quiesced_failure_with_unconfirmed_start": 1,
             "original_stdout_with_orca_banner": 7, "normal_terminations": 6,
             "source_snapshot_note": "Initial SDK probes and subsequent native repair retain separate source manifests; no combined autonomous frozen-candidate claim",
             "cases": records, "original_stdout": stdout_records,
             "cpu_seconds_known_lower_bound": cpu,
             "managed_wall_seconds_known": wall, "resource_cost_complete": False,
             "environment_lease_free": True, "scientific_qualification": False,
             "limitations": "Small developer probes; no global minimum, model reliability, independent NMR reference or seven-task completion claim"}
    target.write_text(json.dumps(audit, indent=2), encoding="utf-8")
    print(json.dumps({key: value for key, value in audit.items()
                      if key not in {"cases", "original_stdout"}}, indent=2))


if __name__ == "__main__":
    main()
