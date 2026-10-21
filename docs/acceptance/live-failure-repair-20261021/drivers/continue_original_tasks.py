"""Run only the remaining original seven tasks, stopping on the first failure."""
import json
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CONTROL = ROOT / "data/live-repair"
PARENT = sys.argv[1] if len(sys.argv) > 1 else "a007"


def read_outcome(folder):
    while True:
        try:
            return json.loads((folder / "outcome.json").read_bytes())
        except (FileNotFoundError, json.JSONDecodeError):
            time.sleep(1)


def reviewable(folder):
    result = read_outcome(folder)
    report = json.loads((folder / "report.json").read_bytes())
    return (not result["error"] and not result["unsettled_execution"]
        and not result["unknown_fields"] and result["source_unchanged"]
        and result["state"] == "completed" and result["delivery_status"] == "complete"
        and report.get("reported_delivery_complete", report["user_goal_complete"]))


def main():
    assert len(PARENT) == 4 and PARENT[0] == "a" and PARENT[1:].isdigit()
    parent_task = json.loads((CONTROL / "attempts" / PARENT / "attempt.json").read_bytes())["task"]
    remaining = sys.argv[2:] or [task for task in ("T1", "T3", "T4", "T5", "H2", "T2", "H1") if task != parent_task]
    assert len(set(remaining)) == len(remaining) and parent_task not in remaining
    assert set(remaining) <= {"T1", "T2", "T3", "T4", "T5", "H1", "H2"}
    marker = CONTROL / ("original-sequence-" + PARENT + "-start.json")
    with marker.open("x", encoding="utf-8") as stream:
        json.dump({"parent": PARENT, "remaining": remaining,
            "stop_on": "first failed, unknown, incomplete or changed-source attempt",
            "independent_raw_review": "required separately; report completion is not scientific qualification"}, stream)
    previous = CONTROL / "attempts" / PARENT
    if not reviewable(previous):
        print(json.dumps({"event": "stopped", "candidate": previous.name, "reason": "not_reviewable"}), flush=True)
        return
    for task in remaining:
        capture = subprocess.run([sys.executable, "-X", "utf8", str(CONTROL / "prepare_attempt.py"), task,
            "--source-candidate", PARENT, "--change", "Verify original task after shared live-failure root repairs; no supplied scientific solution"],
            cwd=ROOT, check=True, capture_output=True, text=True, encoding="utf-8")
        captured = json.loads(capture.stdout)
        folder = Path(captured["attempt"])
        metadata = json.loads((folder / "attempt.json").read_bytes())
        print(json.dumps({"event": "captured", "task": task, "candidate": folder.name}), flush=True)
        completed = subprocess.run([metadata["python"], "-X", "utf8", metadata["driver"], str(folder)],
                                  cwd=metadata["snapshot"])
        if completed.returncode or not reviewable(folder):
            print(json.dumps({"event": "stopped", "candidate": folder.name, "reason": "not_reviewable"}), flush=True)
            return
    print(json.dumps({"event": "all_reports_completed", "independent_raw_review": "pending"}), flush=True)


if __name__ == "__main__":
    main()
