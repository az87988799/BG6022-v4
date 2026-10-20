"""Offline evidence inventory and bounded publication; never executes task code."""

import hashlib
import json
import os
import re
import stat
import sys
import tempfile
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(PROJECT))
BATCH = PROJECT / "data/code-tools-refreeze-1-batch"
DESTINATION = Path(__file__).resolve().parent / "evidence"
TASKS = ("T1", "T2", "T3", "T4", "T5", "H1", "H2")
MAX_FILE_BYTES = 524288
MAX_TOTAL_BYTES = 20 * 1024 * 1024
SUFFIXES = {".json", ".py", ".inp", ".xyz", ".out", ".txt",
            ".csv", ".tsv", ".hess", ".md", ".png"}
SECRET_PATTERNS = (
    re.compile(rb"-----BEGIN (?:[A-Z0-9 ]{0,32})PRIVATE KEY-----"),
    re.compile(rb"https?://[^/\s:@]{1,256}:[^/\s@]{1,256}@"),
    re.compile(rb"\bsk-[A-Za-z0-9_-]{20,512}"),
    re.compile(rb"(?i)\bauthorization[\\\"'\s:=]{1,20}bearer\s+[A-Za-z0-9._~+/-]{16,512}"),
    re.compile(rb"(?i)[\"'](?:api[_-]?key|access[_-]?token|refresh[_-]?token|password)"
               rb"[\"']\s*:\s*[\"'][A-Za-z0-9._~+/=-]{20,512}[\"']"),
)


def checked_path(path, root):
    """Reject links before resolving; scope is a fixed caller-owned directory."""
    path, root = Path(path).absolute(), Path(root).absolute()
    if not path.is_relative_to(root):
        raise RuntimeError("Evidence path is outside its fixed root")
    for part in (path, *path.parents):
        if part.exists() or part.is_symlink():
            info = part.lstat()
            if (stat.S_ISLNK(info.st_mode)
                    or getattr(info, "st_file_attributes", 0) & 0x400):
                raise RuntimeError("Linked/reparse evidence paths are forbidden")
    if not path.resolve().is_relative_to(root.resolve()):
        raise RuntimeError("Resolved evidence path escaped its root")
    return path


def identity(path, secret, *, capture=False):
    """Scan/hash every source in chunks; retain at most one small payload."""
    path = checked_path(path, PROJECT)
    before = path.stat()
    if not stat.S_ISREG(before.st_mode):
        raise RuntimeError("Evidence must be a regular file")
    digest, size, tail = hashlib.sha256(), 0, b""
    chunks = [] if capture and before.st_size <= MAX_FILE_BYTES else None
    overlap = max(4096, len(secret))
    with path.open("rb") as stream:
        while chunk := stream.read(65536):
            window = tail + chunk
            if secret in window or any(pattern.search(window) for pattern in SECRET_PATTERNS):
                raise RuntimeError("Potential credential detected; publication stopped")
            tail = window[-overlap:]
            digest.update(chunk)
            size += len(chunk)
            if chunks is not None:
                if size <= MAX_FILE_BYTES:
                    chunks.append(chunk)
                else:
                    chunks = None
    after = path.stat()
    if ((before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
            != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
            or size != before.st_size):
        raise RuntimeError("Evidence changed during collection")
    return size, digest.hexdigest(), b"".join(chunks) if chunks is not None else None


def read_json(path):
    path = checked_path(path, PROJECT)
    if path.stat().st_size > 4 * 1024 * 1024:
        raise RuntimeError("Identity/control JSON exceeds its read bound")
    return json.loads(path.read_bytes())


def walk_files(root):
    checked_path(root, PROJECT)
    for directory, directories, files in os.walk(root, followlinks=False):
        for name in directories + files:
            checked_path(Path(directory) / name, root)
        for name in sorted(files):
            if Path(name).suffix != ".lock":
                yield Path(directory) / name
        directories.sort()


def main():
    checked_path(DESTINATION, PROJECT)
    if DESTINATION.exists():
        raise RuntimeError("Published evidence already exists; never overwrite it")
    secret = os.environ.get("DEEPSEEK_API_KEY", "").encode()
    if not secret:
        raise RuntimeError("Known credential must be available for the local scan")
    event = read_json(Path(__file__).resolve().parent / "freeze/product-freeze-event.json")
    if Path(event["batch_root"]) != BATCH:
        raise RuntimeError("Batch root differs from the frozen event")
    from tests.helpers.code_tools_batch import snapshot

    ledger = snapshot(BATCH)
    if set(ledger["rows"]) != {f"formal-{task}-001" for task in TASKS}:
        raise RuntimeError("Batch includes missing or additional task/repetition/category slots")
    evidence_root = BATCH / "first-repetition-evidence"
    scope = read_json(evidence_root / "scope.json")
    if (scope["tasks"] != list(TASKS) or scope["repeat"] != 1
            or scope.get("references") != 0 or scope.get("controls") != 0):
        raise RuntimeError("Driver scope differs from the authorized first repetition")
    sources, rows, pending, fingerprints = [], [], [], []
    total = 0

    def collect(path, relative, label, *, required=False):
        nonlocal total
        size, digest, raw = identity(path, secret, capture=True)
        fingerprints.append((path, size, digest))
        entry = {"path": label, "bytes": size, "sha256": digest, "published_path": None}
        excluded = (path.name.endswith((".request.json", ".delivery.json"))
                    or path.name in {"wikidata-entities.json", "pubchem-synonyms.json"}
                    or path.suffix.lower() not in SUFFIXES)
        if raw is not None and not excluded and total + size <= MAX_TOTAL_BYTES:
            entry["published_path"] = (Path("evidence") / relative).as_posix()
            pending.append((relative, raw, entry, required))
            total += size
        elif required:
            raise RuntimeError("Required public driver evidence exceeds publication bounds")
        else:
            entry["not_published_reason"] = "Local original retained; bounded publication policy"
        return entry

    # All required driver receipts exist and match before any destination is written.
    for task in TASKS:
        slot = f"formal-{task}-001"
        directory = evidence_root / task
        start = read_json(directory / "start.json")
        outcome = read_json(directory / "outcome.json")
        prepare_receipt = read_json(directory / "prepare.json")
        prepare = json.loads(prepare_receipt["stdout"])
        workspace = read_json(BATCH / "workspaces" / slot / "workspace.json")
        row = ledger["rows"][slot]
        run_id = start["run_id"]
        if not re.fullmatch(r"run_[a-f0-9]{32}", run_id):
            raise RuntimeError("Invalid Run identity")
        data = checked_path(BATCH / "workspaces" / slot / "data", BATCH)
        if (start["task"] != task or start["repeat"] != 1 or start["slot_id"] != slot
                or Path(start["data_root"]) != data or prepare != workspace
                or Path(workspace["data_root"]) != data or workspace["slot_id"] != slot
                or row["workspace"] != workspace or row["run_id"] != run_id
                or read_json(BATCH / "receipts" / slot / "run.json") != run_id
                or outcome["run_id"] != run_id or row["settlement"] is None
                or prepare_receipt["returncode"] != 0):
            raise RuntimeError("Slot, workspace, receipt or Run binding differs")
        for name in ("pre-verify.json", "post-verify.json"):
            if read_json(directory / name)["returncode"] != 0:
                raise RuntimeError("Frozen candidate verification did not succeed")
        if {path.name for path in (data / "runs").iterdir()} != {run_id}:
            raise RuntimeError("Workspace contains a different or additional Run")
        state = read_json(data / "runs" / run_id / "run.json")
        if state["id"] != run_id or state["state"] != outcome["state"] or state["usage"] != outcome["usage"]:
            raise RuntimeError("Run changed after the driver outcome")
        sources.append((task, directory, data, state))

    global_files = [collect(evidence_root / "scope.json", Path("scope.json"), "scope.json", required=True)]
    for task, directory, data, state in sources:
        driver_files = []
        for name in ("start.json", "outcome.json", "pre-verify.json", "post-verify.json",
                     "prepare.json", "knowledge-audit.json", "budget-check.json",
                     "report.txt", "report.json", "batch-after.json"):
            driver_files.append(collect(directory / name, Path(task) / name, name,
                                        required=name != "report.json" and name != "batch-after.json"))
        rows.append({"task": task, "run_id": state["id"], "local_data_root": str(data),
                     "state": state["state"], "goal_status": state["goal_status"],
                     "delivery_status": state["delivery_status"], "usage": state["usage"],
                     "driver_files": driver_files, "files": []})
    for (task, _, data, _), row in zip(sources, rows):
        for path in sorted(walk_files(data)):
            relative = path.relative_to(data)
            row["files"].append(collect(path, Path(task) / "data" / relative, relative.as_posix()))

    index = {"publication_policy": {"max_payload_file_bytes": MAX_FILE_BYTES,
                                   "max_total_bytes_including_index_and_checksum": MAX_TOTAL_BYTES},
             "published_bytes": total, "global_files": global_files, "tasks": rows,
             "large_originals": "Preserved at indexed local sources",
             "reference_runs": 0, "control_runs": 0,
             "manifest_integrity": "index.sha256 hashes index.json; every payload is indexed"}
    while True:
        index_bytes = (json.dumps(index, ensure_ascii=False, indent=2, allow_nan=False) + "\n").encode()
        checksum = (hashlib.sha256(index_bytes).hexdigest() + "  index.json\n").encode()
        final_size = total + len(index_bytes) + len(checksum)
        if final_size > MAX_TOTAL_BYTES:
            removable = next((i for i in range(len(pending) - 1, -1, -1) if not pending[i][3]), None)
            if removable is None:
                raise RuntimeError("Required evidence and index exceed the total limit")
            _, raw, entry, _ = pending.pop(removable)
            total -= len(raw)
            entry["published_path"] = None
            entry["not_published_reason"] = "Total limit including index; local original retained"
        elif index["published_bytes"] == final_size:
            break
        index["published_bytes"] = total + len(index_bytes) + len(checksum)

    # Detect changing sources again before staging; no partially published package.
    for path, size, digest in fingerprints:
        if identity(path, secret)[:2] != (size, digest):
            raise RuntimeError("Source changed after evidence preflight")
    if secret in index_bytes or any(pattern.search(index_bytes) for pattern in SECRET_PATTERNS):
        raise RuntimeError("Potential credential detected in generated index")
    staging = Path(tempfile.mkdtemp(prefix=".evidence-staging-", dir=DESTINATION.parent))
    checked_path(staging, DESTINATION.parent)
    for relative, raw, entry, _ in pending:
        target = checked_path(staging / relative, staging)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(raw)
        if identity(target, secret)[:2] != (entry["bytes"], entry["sha256"]):
            raise RuntimeError("Staged payload verification failed; staging retained")
    (staging / "index.json").write_bytes(index_bytes)
    (staging / "index.sha256").write_bytes(checksum)
    if (sum(path.stat().st_size for path in walk_files(staging)) != index["published_bytes"]
            or hashlib.sha256((staging / "index.json").read_bytes()).hexdigest()
            != checksum.decode().split()[0]):
        raise RuntimeError("Staged package verification failed; staging retained")
    checked_path(DESTINATION, PROJECT)
    if DESTINATION.exists():
        raise RuntimeError("Destination appeared during collection; staging retained")
    staging.rename(DESTINATION)
    print(json.dumps({"tasks": len(rows), "published_bytes": index["published_bytes"],
                      "index": str(DESTINATION / "index.json"),
                      "index_sha256": checksum.decode().split()[0]}))


if __name__ == "__main__":
    main()
