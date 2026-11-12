"""Read existing, explicitly selected acceptance records without execution or writes."""

import hashlib
import json
from pathlib import Path
from typing import Annotated, Literal

from pydantic import Field

from orca_agent import entrypoints
from orca_agent.models import Digest, Record, RunConfiguration
from orca_agent.report import _safe
from orca_agent.store import Store, StoreError, controlled_path

CATALOG = Path("docs/acceptance/seven-task-browser/catalog.json")
REPOSITORY = Path(__file__).resolve().parents[2]
DOWNLOAD_BYTES = 16 * 1024 * 1024
METADATA_BYTES = 8 * 1024 * 1024
Identifier = Annotated[str, Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]{0,95}$")]
Category = Literal["all", "script", "input", "output", "data"]


class _Variant(Record):
    id: Identifier
    label: Annotated[str, Field(max_length=200)]
    store_root: Annotated[str, Field(max_length=500)]
    run_id: Identifier
    report_path: Annotated[str, Field(max_length=500)]
    report_sha256: Digest
    run_sha256: Digest


class _Task(Record):
    id: Identifier
    title: Annotated[str, Field(max_length=200)]
    summary: Annotated[str, Field(max_length=2000)]
    limitations: Annotated[list[str], Field(max_length=12)] = Field(default_factory=list)
    variants: Annotated[list[_Variant], Field(min_length=1, max_length=4)]


class _Catalog(Record):
    version: Literal[1]
    selection_note: Annotated[str, Field(max_length=2000)] = ""
    tasks: Annotated[list[_Task], Field(max_length=7)]


class _ReadStore(Store):
    """Only the inherited read paths are used; never initialize runtime storage."""

    def __init__(self, root, variant):
        # root has already passed controlled_path, including every ancestor
        # below the trusted repository. Do not call the writable Store initializer.
        if not root.is_dir():
            raise StoreError("saved acceptance workspace is unavailable")
        self.root = root
        self.configuration = RunConfiguration()
        self.environment_root = root
        self._locks = {}
        self._run_id = variant.run_id
        self._run_relative = f"runs/{variant.run_id}/run.json"
        self._run_bytes = _snapshot(self.path(self._run_relative), variant.run_sha256, "Run")

    def _read_json(self, relative):
        if relative == self._run_relative:
            # Every inherited reader in this request consumes the same verified
            # Run bytes, even if the on-disk file is replaced after opening.
            return json.loads(self._run_bytes)
        return super()._read_json(relative)

    def load_run(self, run_id):
        if run_id != self._run_id:
            raise StoreError("Run is outside the selected acceptance source")
        return super().load_run(run_id)

    def _write_json(self, *args, **kwargs):
        raise StoreError("acceptance records are read-only")

    def _lock(self, *args, **kwargs):
        raise StoreError("acceptance records cannot acquire execution locks")


def _catalog(repository):
    path = controlled_path(repository, CATALOG)
    if not path.is_file():
        return _Catalog(version=1, tasks=[])
    if path.stat().st_size > 128 * 1024:
        raise StoreError("acceptance catalog exceeds its read bound")
    catalog = _Catalog.model_validate_json(path.read_bytes())
    if len({task.id for task in catalog.tasks}) != len(catalog.tasks):
        raise StoreError("duplicate acceptance task")
    for task in catalog.tasks:
        if len({variant.id for variant in task.variants}) != len(task.variants):
            raise StoreError("duplicate acceptance source")
    return catalog


def _selection(repository, task_id, variant_id):
    for task in _catalog(repository).tasks:
        if task.id == task_id:
            for variant in task.variants:
                if variant.id == variant_id:
                    return task, variant
    raise StoreError("acceptance task or source is not registered")


def _snapshot(path, expected, kind):
    with path.open("rb") as stream:
        raw = stream.read(METADATA_BYTES + 1)
    if len(raw) > METADATA_BYTES:
        raise StoreError(f"saved acceptance {kind} exceeds its read bound")
    if hashlib.sha256(raw).hexdigest() != expected:
        raise StoreError(f"saved acceptance {kind} differs from its registered snapshot")
    return raw


def _open(repository, variant):
    root = controlled_path(repository, variant.store_root)
    store = _ReadStore(root, variant)
    run = store.load_run(variant.run_id)
    if run.id != variant.run_id:
        raise StoreError("acceptance Run identity does not match the catalog")
    return store, run


def _report(repository, variant, store, run):
    path = controlled_path(repository, variant.report_path)
    raw = _snapshot(path, variant.report_sha256, "report")
    report = json.loads(raw)
    if (report.get("run_id") != run.id or report.get("run_state") != run.state
            or report.get("recorded_delivery_status") != run.delivery_status):
        raise StoreError("saved acceptance report belongs to a different Run snapshot")
    return report


def tasks(repository=REPOSITORY):
    catalog = _catalog(repository)
    rows = []
    for task in catalog.tasks:
        row = task.model_dump(exclude={"variants"})
        row["variants"] = []
        for variant in task.variants:
            source = variant.model_dump(exclude={"store_root"})
            try:
                _open(repository, variant)
                source["available"] = True
            except (ValueError, OSError, RuntimeError):
                source.update(available=False, unavailable_reason="本机原始工作区缺失或记录不可读")
            row["variants"].append(source)
        rows.append(row)
    return {"tasks": rows, "read_only": True, "selection_note": catalog.selection_note}


def detail(task_id, variant_id, *, repository=REPOSITORY):
    task, variant = _selection(repository, task_id, variant_id)
    store, run = _open(repository, variant)
    return _safe({
        "task_id": task.id, "title": task.title, "summary": task.summary,
        "limitations": task.limitations, "read_only": True,
        "variant": variant.model_dump(exclude={"store_root"}),
        "source_root": str(store.root), "original_text": store.load_request(run).original_text,
        "state": run.state, "delivery_status": run.delivery_status,
        "report": _report(repository, variant, store, run),
        "report_basis": "已保存的历史报告；文件当前完整性以逐次读取和下载核验为准",
    }, bounded=False)


def _category(artifact):
    name = Path(artifact.path).name.lower()
    suffix = Path(name).suffix
    if artifact.role == "code" or suffix == ".py":
        return "script"
    if suffix == ".inp" or artifact.role in {
        "input", "native_input", "orca_input", "geometry", "native_dependency",
        "initial_geometry", "prepared_geometry", "prepared_geometry_candidate",
    }:
        return "input"
    if suffix in {".out", ".log", ".gbw", ".hess", ".engrad", ".densities", ".xyz"} or name in {
        "stderr.txt", "stdout.txt",
    }:
        return "output"
    return "data"


def artifacts(task_id, variant_id, *, offset=0, limit=40, category="all", repository=REPOSITORY):
    if not 0 <= offset <= 10000 or not 1 <= limit <= 100 or category not in {
        "all", "script", "input", "output", "data",
    }:
        raise StoreError("acceptance file page outside bounds")
    _, variant = _selection(repository, task_id, variant_id)
    store, run = _open(repository, variant)
    selected = []
    for identifier in sorted(entrypoints.artifact_ids(store, run.id)):
        artifact = store.load_artifact(identifier)
        if category == "all" or _category(artifact) == category:
            selected.append(artifact)
    selected.sort(key=lambda artifact: (Path(artifact.path).name.lower(), artifact.id))
    rows = []
    for artifact in selected[offset:offset + limit]:
        try:
            store.artifact_path(artifact.id)
            integrity = "verified"
        except (ValueError, OSError, RuntimeError):
            integrity = "unavailable"
        rows.append({"id": artifact.id, "path": artifact.path, "role": artifact.role,
                     "size": artifact.size, "sha256": artifact.sha256,
                     "category": _category(artifact), "integrity": {"status": integrity},
                     "downloadable": integrity == "verified" and artifact.size <= DOWNLOAD_BYTES,
                     "download_limit_bytes": DOWNLOAD_BYTES})
    return {"artifacts": rows, "total": len(selected), "offset": offset,
            "next_offset": offset + len(rows) if offset + len(rows) < len(selected) else None,
            "read_only": True}


def text(task_id, variant_id, artifact_id, *, start_line=1, lines=80, repository=REPOSITORY):
    _, variant = _selection(repository, task_id, variant_id)
    store, run = _open(repository, variant)
    return _safe(entrypoints.evidence(store, run.id, "evidence.text", {
        "artifact_id": artifact_id, "start_line": start_line, "lines": lines,
    }), bounded=False)


def download(task_id, variant_id, artifact_id, *, repository=REPOSITORY):
    _, variant = _selection(repository, task_id, variant_id)
    store, run = _open(repository, variant)
    return entrypoints.download(store, run.id, artifact_id)
