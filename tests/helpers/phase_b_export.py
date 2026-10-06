"""Export a bounded, credential-free subset; complete raw evidence stays in its Store."""

import json
import re
from pathlib import Path

from orca_agent.models import utc_now
from orca_agent.store import atomic_write, sha256_file


def export_run(store, run, destination, *, code_basis=None):
    destination = Path(destination)
    destination.mkdir(parents=True, exist_ok=False)
    manifest = {"run_id": run.id, "store_root": str(store.root),
                "exported_at": utc_now().isoformat(), "code_basis": code_basis,
                "scope": "bounded portable subset; omitted binary evidence remains in the original Store/archive",
                "files": [], "omitted_artifacts": []}

    def copy(source, target):
        data = source.read_bytes()
        if re.search(rb"sk-[A-Za-z0-9_-]{16,}|Bearer\s+\S+", data, re.I):
            raise ValueError("credential-like bytes cannot be exported")
        atomic_write(destination / target, data, immutable=True)
        manifest["files"].append({"path": target.as_posix(), "sha256": sha256_file(source),
                                  "bytes": len(data)})

    root = store.path(f"runs/{run.id}")
    for path in sorted(root.rglob("*.json")):
        if "steps" not in path.relative_to(root).parts and path.stat().st_size <= 512 * 1024:
            copy(path, Path("run") / path.relative_to(root))
    request = store.load_request(run)
    artifacts = {request.geometry_artifact_id} | {s.geometry_artifact_id for s in request.systems}
    for result_id in run.result_ids:
        artifacts.update(store.load_result(run.id, result_id).artifact_ids)
    for artifact_id in sorted(artifacts - {None}):
        artifact = store.load_artifact(artifact_id)
        source = store.artifact_path(artifact_id)
        copy(store.path(f"artifacts/{artifact_id}/artifact.json"),
             Path("artifacts") / artifact_id / "artifact.json")
        if source.suffix.lower() in {".json", ".out", ".inp", ".xyz", ".txt"} and artifact.size <= 512 * 1024:
            copy(source, Path("artifacts") / artifact_id / source.name)
        else:
            manifest["omitted_artifacts"].append({"id": artifact_id, "sha256": artifact.sha256,
                                                  "bytes": artifact.size, "role": artifact.role})
    atomic_write(destination / "manifest.json",
                 (json.dumps(manifest, ensure_ascii=False, indent=2) + "\n").encode(), immutable=True)
    return manifest
