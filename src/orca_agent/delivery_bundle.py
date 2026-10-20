"""Explicit, bounded export of an existing delivery; never executes or repairs it."""

import copy
import hashlib
import json
import os
import re
import tempfile
import zipfile
from dataclasses import asdict, dataclass
from pathlib import Path

from orca_agent.entrypoints import artifact_ids
from orca_agent.models import fingerprint
from orca_agent.report import build_report, render_report
from orca_agent.store import StoreError, _is_link, controlled_path, sha256_file


@dataclass(frozen=True)
class BundleLimits:
    """Limits may be reduced by local callers, never raised past the fixed ceiling."""

    max_files: int = 256
    max_file_bytes: int = 16 * 1024 * 1024
    max_total_bytes: int = 64 * 1024 * 1024

    def __post_init__(self):
        for key, ceiling in (
            ("max_files", 256), ("max_file_bytes", 16 * 1024 * 1024),
            ("max_total_bytes", 64 * 1024 * 1024),
        ):
            value = getattr(self, key)
            if type(value) is not int or not 1 <= value <= ceiling:
                raise ValueError("invalid delivery bundle limit")


def _json(value):
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2,
                       allow_nan=False) + "\n").encode("utf-8")


def _destination(store, output):
    candidate = Path(output).absolute()
    if candidate.suffix.casefold() != ".zip" or not candidate.parent.is_dir():
        raise StoreError("bundle target requires an existing directory and a .zip filename")
    # Check every component before resolve, including Windows junctions and ADS.
    root = Path(candidate.anchor)
    target = controlled_path(root, candidate.relative_to(root))
    if _is_link(root) or target.exists():
        raise StoreError("bundle target already exists or is a link")
    target = target.resolve()
    for protected in (store.root, store.environment_root):
        if target.is_relative_to(protected.resolve()):
            raise StoreError("bundle target cannot be inside historical or environment storage")
    return target


def _basename(path):
    """Only a conservative, portable filename becomes a ZIP member component."""
    name = path.name
    reserved = re.fullmatch(r"(?:CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])",
                            name.split(".")[0], re.IGNORECASE)
    if (not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,95}", name)
            or name.endswith(".") or reserved):
        suffix = path.suffix if re.fullmatch(r"\.[A-Za-z0-9]{1,8}", path.suffix) else ".bin"
        return "artifact" + suffix
    return name


class _BoundedReportStore:
    """Apply export permission and byte bounds before the report hashes any file."""

    def __init__(self, store, allowed, limits):
        self.store, self.allowed, self.limits = store, allowed, limits
        self.sources = {}
        self.source_bytes = 0
        self.rejected = False

    def __getattr__(self, name):
        return getattr(self.store, name)

    def artifact_path(self, identifier):
        try:
            if identifier not in self.allowed:
                raise StoreError("report references an Artifact outside current Run read permission")
            artifact = self.store.load_artifact(identifier)
            if identifier in self.sources:
                saved = self.sources[identifier]
                if artifact != saved["artifact"]:
                    raise StoreError("Artifact metadata changed during export")
                return saved["path"]
            if (len(self.sources) + 5 > self.limits.max_files
                    or artifact.size > self.limits.max_file_bytes
                    or self.source_bytes + artifact.size > self.limits.max_total_bytes):
                raise StoreError("delivery sources exceed the export file or byte bound")
            path = self.store.artifact_path(identifier)
            self.sources[identifier] = {"artifact": artifact, "path": path}
            self.source_bytes += artifact.size
            return path
        except (ValueError, OSError, RuntimeError):
            self.rejected = True
            raise


def _portable_report(report, entries):
    """Rebase only artifact links; evidence, values and qualification are unchanged."""
    portable = copy.deepcopy(report)
    for rows in (
        portable.get("artifact_index", []), portable.get("generated_scripts", []),
        *(result.get("artifacts", []) for result in portable["results"]),
    ):
        for row in rows:
            entry = entries.get(row["artifact_id"])
            if entry and row.get("status") == "verified":
                row["path"] = entry["archive_path"]
    return portable


def _zip_info(name):
    if (name.startswith("/") or "\\" in name or ":" in name
            or any(part in {"", ".", ".."} for part in name.split("/"))):
        raise StoreError("invalid ZIP entry name")
    info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
    info.compress_type = zipfile.ZIP_DEFLATED
    info.create_system = 3
    info.external_attr = 0o100644 << 16
    return info


def _copy_source(archive, name, path, artifact):
    digest, size = hashlib.sha256(), 0
    with path.open("rb") as reader, archive.open(_zip_info(name), "w") as writer:
        for chunk in iter(lambda: reader.read(1024 * 1024), b""):
            size += len(chunk)
            if size > artifact.size:
                raise StoreError("Artifact grew during export")
            digest.update(chunk)
            writer.write(chunk)
    if size != artifact.size or digest.hexdigest() != artifact.sha256:
        raise StoreError("Artifact bytes changed during export")


def _verify_archive(path, files, limits):
    expected = {row["archive_path"]: row for row in files}
    with zipfile.ZipFile(path) as archive:
        names = archive.namelist()
        if len(names) != len(expected) or set(names) != set(expected):
            raise StoreError("bundle archive member set differs")
        total = 0
        for info in archive.infolist():
            row = expected[info.filename]
            total += info.file_size
            if (info.file_size != row["size_bytes"] or info.file_size > limits.max_file_bytes
                    or total > limits.max_total_bytes):
                raise StoreError("bundle archive size differs or exceeds its bound")
            digest = hashlib.sha256()
            with archive.open(info) as reader:
                for chunk in iter(lambda: reader.read(1024 * 1024), b""):
                    digest.update(chunk)
            if digest.hexdigest() != row["sha256"]:
                raise StoreError("bundle archive content hash differs")


def export_bundle(store, run_id: str, output: str | Path, *,
                  limits: BundleLimits | None = None) -> dict:
    """Write one new ZIP outside Store; snapshot and original bytes stay unchanged.

    A scientifically failed/unverified result is exportable with its original
    labels. A referenced missing, unauthorized or corrupt file fails the whole
    export. No partial ZIP is published and existing targets are never replaced.
    """
    limits = limits or BundleLimits()
    target = _destination(store, output)
    run = store.load_run(run_id)
    # build_report has a 128-result display bound. Do not export a truncated Run.
    if len(run.result_ids) > 128:
        raise StoreError("Run exceeds the existing delivery report result bound")
    run_hash = fingerprint(run.model_dump(mode="json"))
    control = store.read_control(run_id)
    allowed = artifact_ids(store, run_id)
    bounded = _BoundedReportStore(store, allowed, limits)
    report = build_report(bounded, run)
    if bounded.rejected or any(row.get("status") != "verified"
                               for row in report["artifact_index"]):
        raise StoreError("delivery contains missing, unauthorized, corrupt or over-limit files")
    entries, payloads = {}, {}
    for row in sorted(report["artifact_index"], key=lambda item: item["artifact_id"]):
        identifier = row["artifact_id"]
        saved = bounded.sources.get(identifier)
        if not saved or identifier not in allowed:
            raise StoreError("delivery source was not authorized and verified")
        artifact, path = saved["artifact"], saved["path"]
        if row["sha256"] != artifact.sha256:
            raise StoreError("delivery source hash binding differs")
        # Equal immutable bytes share one member; each Artifact identity remains.
        key = (artifact.sha256, artifact.size)
        if key not in payloads:
            name = f"artifacts/{artifact.sha256}/{_basename(path)}"
            payloads[key] = {"name": name, "path": path, "artifact": artifact}
        entries[identifier] = {
            "artifact_id": identifier, "sha256": artifact.sha256,
            "size_bytes": artifact.size, "role": row["role"],
            "run_id": artifact.run_id, "attempt_id": artifact.attempt_id,
            "purposes": row["purposes"], "source_integrity": row["status"],
            "archive_path": payloads[key]["name"],
        }
    portable = _portable_report(report, entries)
    validation = {
        "file_integrity": "SHA256 and size verified against registered Artifact metadata",
        "qualification_policy": "Export does not grant or change scientific qualification.",
        "model_or_orca_execution": False, "automatic_repair": False,
        "run_state": report["run_state"],
        "recorded_delivery_status": report["recorded_delivery_status"],
        "report_delivery_status": report["report_delivery_status"],
        "user_goal_complete": report["user_goal_complete"],
        "goals": report["goals"], "gaps": report["gaps"],
        "results": [
            {key: result.get(key) for key in (
                "run_id", "result_id", "operation_status", "scientific_status",
                "output_validation", "gaps",
            )} for result in report["results"]
        ],
        "limitations": report["limitations"] + [
            "The bundle is a bounded snapshot, not a new scientific check or model explanation.",
            "Report omissions, failed outputs and unverified observations retain their labels.",
            "Only report-indexed files authorized for this Run are included.",
        ],
    }
    documents = {
        "report.json": _json(portable),
        "report.md": render_report(portable).encode("utf-8"),
        "validation.json": _json(validation),
    }
    files = [{"archive_path": item["name"], "size_bytes": item["artifact"].size,
              "sha256": item["artifact"].sha256} for item in payloads.values()]
    files += [{"archive_path": name, "size_bytes": len(data),
               "sha256": hashlib.sha256(data).hexdigest()} for name, data in documents.items()]
    manifest = {
        "schema_version": "delivery-bundle-1", "run_id": run_id,
        "run_snapshot_sha256": run_hash, "control_generation": control["generation"],
        "limits": asdict(limits), "source_artifact_count": len(entries),
        "source_bytes_including_duplicate_artifacts": bounded.source_bytes,
        "archive_file_count": len(files) + 1, "sources": list(entries.values()),
        "files": sorted(files, key=lambda item: item["archive_path"]),
        "report_projection": "Only artifact link paths are rebased into this archive.",
        "qualification": validation,
    }
    documents["manifest.json"] = _json(manifest)
    files.append({"archive_path": "manifest.json", "size_bytes": len(documents["manifest.json"]),
                  "sha256": hashlib.sha256(documents["manifest.json"]).hexdigest()})
    total_bytes = sum(item["size_bytes"] for item in files)
    if (len(files) > limits.max_files or total_bytes > limits.max_total_bytes
            or any(item["size_bytes"] > limits.max_file_bytes for item in files)):
        raise StoreError("delivery documents and files exceed export bounds")
    temporary = None
    try:
        # A sibling temporary file makes the no-replace hardlink publication atomic.
        descriptor, name = tempfile.mkstemp(prefix=".orca-bundle-", suffix=".tmp",
                                            dir=target.parent)
        temporary = Path(name)
        with os.fdopen(descriptor, "w+b") as writer:
            with zipfile.ZipFile(writer, "w", compression=zipfile.ZIP_DEFLATED,
                                 compresslevel=6, allowZip64=False) as archive:
                for item in sorted(payloads.values(), key=lambda item: item["name"]):
                    _copy_source(archive, item["name"], item["path"], item["artifact"])
                for name, content in sorted(documents.items()):
                    archive.writestr(_zip_info(name), content)
            writer.flush()
            os.fsync(writer.fileno())
        if temporary.stat().st_size > limits.max_total_bytes + limits.max_files * 512:
            raise StoreError("compressed bundle exceeds its archive byte bound")
        _verify_archive(temporary, files, limits)
        for identifier, item in bounded.sources.items():
            if store.load_artifact(identifier) != item["artifact"]:
                raise StoreError("Artifact metadata changed during export")
            store.artifact_path(identifier)
        if (fingerprint(store.load_run(run_id).model_dump(mode="json")) != run_hash
                or store.read_control(run_id) != control
                or artifact_ids(store, run_id) != allowed):
            raise StoreError("Run or read permission changed during export; retry explicitly")
        if _destination(store, target) != target:
            raise StoreError("bundle destination changed during export")
        archive_hash = sha256_file(temporary)
        archive_size = temporary.stat().st_size
        # No replace/rename fallback: unsupported filesystems fail without a final ZIP.
        os.link(temporary, target)
        return {
            "run_id": run_id, "path": str(target), "sha256": archive_hash,
            "archive_bytes": archive_size, "uncompressed_bytes": total_bytes,
            "archive_file_count": len(files), "source_artifact_count": len(entries),
            "report_delivery_status": report["report_delivery_status"],
            "user_goal_complete": report["user_goal_complete"],
            "model_calls": 0, "orca_starts": 0,
        }
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
