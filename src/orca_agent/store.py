"""Small file store: immutable evidence, cumulative budgets and durable leases.

An OS lock protects each mutation; a durable environment lease survives lock-owner
death. A missing process handle never proves that a calculation did not start.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from datetime import timedelta
from pathlib import Path, PureWindowsPath
from typing import Any
from uuid import uuid4

from filelock import FileLock

from orca_agent.models import (
    Artifact,
    Attempt,
    BudgetLimits,
    PermissionSnapshot,
    Plan,
    Request,
    Result,
    Run,
    Step,
    fingerprint,
    new_id,
    utc_now,
)
from orca_agent.tools.registry import get_tool, validate_geometry, validate_parameters


class StoreError(ValueError):
    """A stored fact or requested operation violates a boundary."""


class BudgetExceeded(StoreError):
    """An immutable cumulative budget prevents a new launch."""


class EnvironmentBusy(StoreError):
    """An existing lease has not been demonstrably released."""


def _id(value: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,95}", value):
        raise StoreError("invalid object identity")
    return value


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _is_link(path: Path) -> bool:
    if path.is_symlink() or (hasattr(path, "is_junction") and path.is_junction()):
        return True
    try:
        attributes = getattr(path.lstat(), "st_file_attributes", 0)
    except FileNotFoundError:
        return False
    # Python 3.11 has no Path.is_junction(). Reject all Windows reparse points,
    # including junctions, rather than silently following one inside the root.
    return bool(attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))


def controlled_path(root: Path, relative: str | Path) -> Path:
    """Reject escapes, ADS paths, junctions and symlinks before touching a file."""
    text = str(relative).replace("\\", "/")
    windows = PureWindowsPath(text)
    parts = text.split("/")
    if (
        not text or text.startswith("/") or windows.drive or windows.root
        or any(part in ("", ".", "..") for part in parts)
        or any(":" in part or part.endswith((".", " ")) for part in parts)
    ):
        raise StoreError("path must be a normalized path inside the controlled root")
    for part in parts:
        stem = part.split(".")[0].upper()
        if stem in {"CON", "PRN", "AUX", "NUL"} or re.fullmatch(r"(?:COM|LPT)[1-9]", stem):
            raise StoreError("reserved device path")
    current = root
    if _is_link(current):
        raise StoreError("controlled root cannot be a link")
    for part in parts:
        current = current / part
        if _is_link(current):
            raise StoreError("links are not permitted inside controlled paths")
    try:
        current.resolve().relative_to(root.resolve())
    except ValueError as exc:
        raise StoreError("path escapes the controlled root") from exc
    return current


def atomic_write(path: Path, data: bytes, *, immutable: bool = False) -> None:
    """Publish a complete file; immutable publication never replaces an existing name."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.parent / f".w-{uuid4().hex[:16]}.tmp"
    try:
        with temporary.open("xb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        if immutable:
            os.link(temporary, path)
        else:
            os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _json_bytes(value: Any) -> bytes:
    if hasattr(value, "model_dump"):
        value = value.model_dump(mode="json")
    return (json.dumps(value, indent=2, ensure_ascii=False, allow_nan=False) + "\n").encode("utf-8")


def default_environment_root() -> Path:
    """A machine-user execution environment, independent of project/run/data roots."""
    base = Path(os.environ.get("LOCALAPPDATA", str(Path.home() / ".local" / "share")))
    return base / "orca-agent" / "environment"


class Store:
    def __init__(self, root: str | Path, *, environment_root: str | Path | None = None):
        original = Path(root).absolute()
        if _is_link(original):
            raise StoreError("data root cannot be a link")
        self.root = original.resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        # The override is dependency injection for isolated tests. Production CLI
        # intentionally does not expose it as a flag or a configuration setting.
        self.environment_root = Path(environment_root or default_environment_root()).absolute()
        if _is_link(self.environment_root):
            raise StoreError("environment root cannot be a link")
        self.environment_root.mkdir(parents=True, exist_ok=True)
        self._locks: dict[str, FileLock] = {}

    def path(self, relative: str | Path) -> Path:
        return controlled_path(self.root, relative)

    def _lock(self, path: Path) -> FileLock:
        key = str(path)
        if key not in self._locks:
            path.parent.mkdir(parents=True, exist_ok=True)
            self._locks[key] = FileLock(path, timeout=0)
        return self._locks[key]

    def run_lock(self, run_id: str) -> FileLock:
        return self._lock(self.path(f"runs/{_id(run_id)}/coordinator.lock"))

    def control_lock(self, run_id: str) -> FileLock:
        """Serialize a control signal against final validation and process resume."""
        path = self.path(f"runs/{_id(run_id)}/control.lock")
        key = str(path)
        if key not in self._locks:
            path.parent.mkdir(parents=True, exist_ok=True)
            self._locks[key] = FileLock(path, timeout=5)
        return self._locks[key]

    def _environment_lock(self) -> FileLock:
        return self._lock(controlled_path(self.environment_root, "environment.lock"))

    def _write_json(self, relative: str, value: Any, *, immutable: bool = False) -> None:
        destination = self.path(relative)
        content = _json_bytes(value)
        if immutable and destination.exists():
            if destination.read_bytes() == content:
                return
            raise StoreError(f"immutable record already exists: {relative}")
        atomic_write(destination, content, immutable=immutable)

    def _read_json(self, relative: str) -> Any:
        destination = self.path(relative)
        if destination.stat().st_size > 8 * 1024 * 1024:
            raise StoreError("metadata exceeds the 8 MiB read limit")
        return json.loads(destination.read_text(encoding="utf-8"))

    def create_run(
        self, request: Request, plan: Plan,
        permission: PermissionSnapshot | None = None, budget: BudgetLimits | None = None,
    ) -> Run:
        request = Request.model_validate(request.model_dump())
        plan = Plan.model_validate(plan.model_dump())
        plan.validate_request(request)
        initial = self.artifact_path(request.geometry_artifact_id)
        if initial.stat().st_size > 65536:
            raise StoreError("geometry exceeds the 64 KiB input limit")
        for step in plan.steps:
            params = validate_parameters(step.tool, step.parameters)
            validate_geometry(initial.read_text(encoding="utf-8"), params)
        permission = permission or PermissionSnapshot(artifact_ids=[request.geometry_artifact_id])
        permission = PermissionSnapshot.model_validate(permission.model_dump())
        if request.geometry_artifact_id not in permission.artifact_ids:
            raise StoreError("initial geometry is outside the permission snapshot")
        budget = budget or BudgetLimits()
        budget = BudgetLimits.model_validate(budget.model_dump())
        run = Run(
            request_id=request.id, request_version=request.version,
            plan_id=plan.id, plan_version=plan.version, permission=permission, budget=budget,
            deadline=utc_now() + timedelta(seconds=budget.run_seconds),
            goal_status={goal.id: "insufficient_evidence" for goal in request.goals},
        )
        self._write_json(
            f"runs/{run.id}/request-revisions/{request.version}.json", request, immutable=True
        )
        self._write_json(f"runs/{run.id}/plan-revisions/{plan.version}.json", plan, immutable=True)
        self._write_json(f"runs/{run.id}/permission.json", permission, immutable=True)
        self._write_json(f"runs/{run.id}/budget.json", budget, immutable=True)
        self._write_json(f"runs/{run.id}/run.json", run, immutable=True)
        return run

    def load_run(self, run_id: str) -> Run:
        run = Run.model_validate(self._read_json(f"runs/{_id(run_id)}/run.json"))
        if run.id != run_id:
            raise StoreError("run identity does not match its location")
        return run

    def load_request(self, run: Run | str) -> Request:
        run = self.load_run(run) if isinstance(run, str) else run
        request = Request.model_validate(
            self._read_json(f"runs/{_id(run.id)}/request-revisions/{run.request_version}.json")
        )
        if (request.id, request.version) != (run.request_id, run.request_version):
            raise StoreError("request revision identity mismatch")
        return request

    def load_plan(self, run: Run | str) -> Plan:
        run = self.load_run(run) if isinstance(run, str) else run
        plan = Plan.model_validate(
            self._read_json(f"runs/{_id(run.id)}/plan-revisions/{run.plan_version}.json")
        )
        if (plan.id, plan.version) != (run.plan_id, run.plan_version):
            raise StoreError("plan revision identity mismatch")
        plan.validate_request(self.load_request(run))
        return plan

    def save_run(self, run: Run) -> None:
        run = Run.model_validate(run.model_dump())
        with self.run_lock(run.id):
            previous = self.load_run(run.id)
            for field in ("request_id", "request_version", "plan_id", "plan_version", "permission",
                          "budget", "created_at", "deadline"):
                if getattr(run, field) != getattr(previous, field):
                    raise StoreError(f"fixed run field cannot be changed: {field}")
            for field in ("orca_starts_reserved", "orca_starts_actual", "postprocess_starts",
                          "elapsed_seconds", "cpu_seconds"):
                if getattr(run.usage, field) < getattr(previous.usage, field):
                    raise StoreError(f"cumulative usage cannot decrease: {field}")
            for field in ("logical_attempts", "fingerprint_attempts"):
                old, new = getattr(previous.usage, field), getattr(run.usage, field)
                if any(new.get(key, 0) < value for key, value in old.items()):
                    raise StoreError("attempt counters cannot be reset")
            if not previous.usage.resource_usage_complete and run.usage.resource_usage_complete:
                raise StoreError("unknown historical resource usage cannot be silently declared complete")
            if len(run.attempts) < len(previous.attempts):
                raise StoreError("attempt history cannot be removed")
            for old, new in zip(previous.attempts, run.attempts, strict=False):
                for field in ("id", "step_id", "logical_id", "number", "tool", "geometry_artifact_id",
                              "input_fingerprint", "directory", "created_at"):
                    if getattr(old, field) != getattr(new, field):
                        raise StoreError(f"attempt identity/input cannot be rewritten: {field}")
            if not set(previous.result_ids).issubset(run.result_ids):
                raise StoreError("result references cannot be removed")
            self._write_json(f"runs/{run.id}/run.json", run)

    def import_artifact(
        self, path: str | Path, role: str, *, run_id: str | None = None,
        attempt_id: str | None = None, source: dict | None = None,
    ) -> Artifact:
        original = Path(path)
        if not original.is_file() or _is_link(original):
            raise StoreError("artifact source must be an existing regular, non-link file")
        artifact_id = new_id("artifact")
        relative = f"artifacts/{artifact_id}/files/{original.name}"
        destination = self.path(relative)
        before_hash = sha256_file(original)
        destination.parent.mkdir(parents=True, exist_ok=False)
        # Stream rather than load GBW files into memory. Metadata publication occurs last.
        with original.open("rb") as reader, destination.open("xb") as writer:
            for block in iter(lambda: reader.read(1024 * 1024), b""):
                writer.write(block)
            writer.flush()
            os.fsync(writer.fileno())
        copied_hash = sha256_file(destination)
        if copied_hash != before_hash or sha256_file(original) != before_hash:
            raise StoreError("artifact source changed while creating its immutable snapshot")
        artifact = Artifact(
            id=artifact_id, path=relative, sha256=copied_hash, size=destination.stat().st_size,
            role=role, run_id=run_id, attempt_id=attempt_id,
            source=source or {"imported_from": str(original.resolve())},
        )
        self._write_json(f"artifacts/{artifact.id}/artifact.json", artifact, immutable=True)
        return artifact

    def load_artifact(self, artifact_id: str) -> Artifact:
        artifact = Artifact.model_validate(
            self._read_json(f"artifacts/{_id(artifact_id)}/artifact.json")
        )
        if artifact.id != artifact_id or not artifact.path.startswith(f"artifacts/{artifact_id}/"):
            raise StoreError("artifact identity/location mismatch")
        return artifact

    def artifact_path(self, artifact_id: str, *, verify: bool = True) -> Path:
        artifact = self.load_artifact(artifact_id)
        path = self.path(artifact.path)
        if verify and (path.stat().st_size != artifact.size or sha256_file(path) != artifact.sha256):
            raise StoreError("artifact hash changed; prior bindings and checks are invalid")
        return path

    def environment_lease(self) -> dict[str, Any] | None:
        path = controlled_path(self.environment_root, "lease.json")
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None

    def reserve_attempt(self, run: Run, step: Step, geometry_artifact_id: str) -> Attempt:
        """Reserve once before any process creation; caller holds the coordinator lock."""
        with self.run_lock(run.id), self._environment_lock():
            current = self.load_run(run.id)
            if current.model_dump() != run.model_dump():
                raise StoreError("stale run snapshot; reload and revalidate")
            if self.read_signal(run.id) in ("pause", "cancel"):
                raise StoreError("pause/cancel requested before submission")
            if run.state not in ("ready", "running", "paused"):
                raise StoreError(f"run state does not permit a launch: {run.state}")
            plan = self.load_plan(run)
            actual = next((item for item in plan.steps if item.id == step.id), None)
            if actual is None or actual != step:
                raise StoreError("step differs from its immutable plan revision")
            permission = PermissionSnapshot.model_validate(
                self._read_json(f"runs/{run.id}/permission.json")
            )
            if permission != run.permission or not permission.scientific_execution:
                raise StoreError("scientific execution is not authorized")
            if self._read_json(f"runs/{run.id}/budget.json") != run.budget.model_dump(mode="json"):
                raise StoreError("budget snapshot mismatch")
            params = validate_parameters(step.tool, step.parameters)
            if step.tool not in permission.allowed_tools:
                raise StoreError("tool is outside the permission snapshot")
            if params.cores > permission.max_cores or params.memory_mb > permission.max_memory_mb:
                raise StoreError("resources exceed the permission snapshot")
            geometry = self.load_artifact(geometry_artifact_id)
            geometry_path = self.artifact_path(geometry_artifact_id)
            if geometry.size > 65536:
                raise StoreError("geometry exceeds the 64 KiB input limit")
            validate_geometry(geometry_path.read_text(encoding="utf-8"), params)
            if step.geometry.artifact_id:
                if geometry_artifact_id != step.geometry.artifact_id:
                    raise StoreError("geometry differs from the plan binding")
                if geometry_artifact_id not in permission.artifact_ids:
                    raise StoreError("geometry is not authorized")
            else:
                self._validate_future_binding(run, step, geometry_artifact_id)
            for dependency in step.depends_on:
                if not any(a.step_id == dependency and a.result_id for a in run.attempts):
                    raise StoreError("dependency has no persisted result")
            input_fingerprint = fingerprint({
                "tool": step.tool, "parameters": params.model_dump(), "geometry_hash": geometry.sha256,
            })
            if utc_now() >= run.deadline:
                raise BudgetExceeded("run deadline exhausted; resume cannot reset it")
            if run.usage.orca_starts_reserved >= run.budget.orca_starts:
                raise BudgetExceeded("total ORCA launch budget exhausted")
            number = run.usage.logical_attempts.get(step.logical_id, 0) + 1
            same_input_number = run.usage.fingerprint_attempts.get(input_fingerprint, 0) + 1
            if max(number, same_input_number) > run.budget.attempts_per_step:
                raise BudgetExceeded("logical/input attempt budget exhausted")
            repeated = sum(max(0, count - 1) for count in run.usage.logical_attempts.values())
            if number > 1 and repeated >= run.budget.extra_orca_starts:
                raise BudgetExceeded("extra ORCA launch budget exhausted")
            for prior in run.attempts:
                if prior.state in ("intent", "running", "unknown"):
                    raise EnvironmentBusy("previous attempt still needs reconciliation")
                if prior.input_fingerprint == input_fingerprint and prior.state in ("failed", "timed_out"):
                    raise StoreError("unchanged failed input cannot be retried automatically")
            if self.environment_lease() is not None:
                raise EnvironmentBusy("environment quota is occupied, including unknown old attempts")
            attempt = Attempt(
                step_id=step.id, logical_id=step.logical_id, number=number, tool=step.tool,
                geometry_artifact_id=geometry_artifact_id, input_fingerprint=input_fingerprint,
                directory=f"runs/{run.id}/steps/{step.id}/attempt-{number:03d}",
            )
            self.path(attempt.directory).mkdir(parents=True, exist_ok=False)
            self._write_json(f"{attempt.directory}/intent.json", {
                "run_id": run.id, "attempt": attempt.model_dump(mode="json"),
                "parameters": params.model_dump(), "request_version": run.request_version,
                "plan_version": run.plan_version, "permission_version": permission.version,
            }, immutable=True)
            lease = {"run_id": run.id, "attempt_id": attempt.id, "data_root": str(self.root),
                     "input_fingerprint": input_fingerprint, "state": "intent",
                     "created_at": utc_now().isoformat()}
            atomic_write(controlled_path(self.environment_root, "lease.json"), _json_bytes(lease))
            run.attempts.append(attempt)
            run.usage.orca_starts_reserved += 1
            run.usage.logical_attempts[step.logical_id] = number
            run.usage.fingerprint_attempts[input_fingerprint] = same_input_number
            run.state = "running"
            self.save_run(run)
            return attempt

    def _validate_future_binding(self, run: Run, step: Step, artifact_id: str) -> None:
        producer = step.geometry.producer_step_id
        for attempt in run.attempts:
            if attempt.step_id != producer or not attempt.result_id:
                continue
            result = self.load_result(run.id, attempt.result_id)
            output = result.qualified_outputs.get(step.geometry.port)
            if output and output.artifact_id == artifact_id:
                if result.attempt_id != attempt.id:
                    raise StoreError("producer result/attempt mismatch")
                if any(check.rule_version != get_tool(step.tool).check_version for check in output.checks):
                    raise StoreError("producer geometry checks do not meet the consumer's rule version")
                return
        raise StoreError("future geometry is not a concrete qualified producer output")

    def update_lease_handle(self, run_id: str, attempt_id: str, handle: dict[str, Any]) -> None:
        with self._environment_lock():
            lease = self.environment_lease()
            if not lease or (lease["run_id"], lease["attempt_id"]) != (run_id, attempt_id):
                raise StoreError("cannot update a different environment lease")
            lease.update(state="running", execution_handle=handle)
            atomic_write(controlled_path(self.environment_root, "lease.json"), _json_bytes(lease))

    def release_environment(self, run_id: str, attempt_id: str, *, termination_confirmed: bool) -> None:
        if not termination_confirmed:
            raise EnvironmentBusy("unconfirmed termination retains the environment lease")
        with self._environment_lock():
            lease = self.environment_lease()
            if lease is None:
                return
            if (lease["run_id"], lease["attempt_id"]) != (run_id, attempt_id):
                raise StoreError("cannot release another attempt's environment lease")
            controlled_path(self.environment_root, "lease.json").unlink()

    def save_result(self, result: Result) -> None:
        result = Result.model_validate(result.model_dump())
        run = self.load_run(result.run_id)
        attempt = next((a for a in run.attempts if a.id == result.attempt_id), None)
        if attempt is None or attempt.step_id != result.step_id:
            raise StoreError("result does not belong to a stored attempt")
        if set(result.qualified_outputs) - set(get_tool(attempt.tool).output_ports):
            raise StoreError("result publishes a port not declared by its tool")
        for artifact_id in result.artifact_ids:
            artifact = self.load_artifact(artifact_id)
            self.artifact_path(artifact_id)
            if (artifact.run_id, artifact.attempt_id) != (run.id, attempt.id):
                raise StoreError("result artifact provenance mismatch")
        for output in result.qualified_outputs.values():
            if output.artifact_id and output.artifact_id not in result.artifact_ids:
                raise StoreError("qualified artifact is absent from the result evidence")
        self._write_json(f"runs/{run.id}/results/{result.id}.json", result, immutable=True)

    def load_result(self, run_id: str, result_id: str) -> Result:
        result = Result.model_validate(
            self._read_json(f"runs/{_id(run_id)}/results/{_id(result_id)}.json")
        )
        if result.id != result_id or result.run_id != run_id:
            raise StoreError("result identity/location mismatch")
        return result

    def finish_attempt(
        self, run: Run, attempt_id: str, *, state: str, result_id: str | None = None,
        elapsed_seconds: float = 0, cpu_seconds: float = 0, started: bool = True,
        termination_confirmed: bool,
    ) -> None:
        attempt = next(a for a in run.attempts if a.id == attempt_id)
        if result_id:
            result = self.load_result(run.id, result_id)
            if result.attempt_id != attempt.id:
                raise StoreError("result refers to another attempt")
        if attempt.finished_at is not None:
            raise StoreError("attempt was already settled")
        if elapsed_seconds < attempt.elapsed_seconds or cpu_seconds < attempt.cpu_seconds:
            raise StoreError("attempt resource usage cannot decrease during reconciliation")
        run.usage.orca_starts_actual += int(started and not attempt.started)
        run.usage.elapsed_seconds += elapsed_seconds - attempt.elapsed_seconds
        run.usage.cpu_seconds += cpu_seconds - attempt.cpu_seconds
        attempt.started = attempt.started or started
        attempt.state = state if termination_confirmed else "unknown"
        attempt.finished_at = utc_now() if termination_confirmed else None
        attempt.result_id = result_id
        attempt.elapsed_seconds = elapsed_seconds
        attempt.cpu_seconds = cpu_seconds
        if result_id and result_id not in run.result_ids:
            run.result_ids.append(result_id)
        self.save_run(run)
        if termination_confirmed:
            self.release_environment(run.id, attempt.id, termination_confirmed=True)

    def signal(self, run_id: str, action: str | None) -> None:
        self.load_run(run_id)
        if action not in (None, "pause", "cancel"):
            raise StoreError("unknown control signal")
        with self.control_lock(run_id):
            self._write_json(f"runs/{_id(run_id)}/control.json", {
                "action": action, "created_at": utc_now().isoformat(),
            })

    def read_signal(self, run_id: str) -> str | None:
        relative = f"runs/{_id(run_id)}/control.json"
        return self._read_json(relative)["action"] if self.path(relative).exists() else None

    def record_diagnostic(self, run: Run, category: str, message: str) -> None:
        run.diagnostics.append({"category": category, "message": message,
                                "created_at": utc_now().isoformat()})
        self.save_run(run)

    def integrity_issues(self, run_id: str) -> list[str]:
        """Discover missing references and orphan results without launching anything."""
        run = self.load_run(run_id)
        issues: list[str] = []
        self.load_plan(run)
        try:
            self.artifact_path(self.load_request(run).geometry_artifact_id)
        except (OSError, ValueError) as exc:
            issues.append(f"initial geometry is invalid: {exc}")
        for result_id in run.result_ids:
            try:
                result = self.load_result(run.id, result_id)
                for artifact_id in result.artifact_ids:
                    self.artifact_path(artifact_id)
            except (OSError, ValueError) as exc:
                issues.append(f"invalid result reference {result_id}: {exc}")
        result_dir = self.path(f"runs/{run.id}/results")
        if result_dir.exists():
            for path in result_dir.glob("*.json"):
                if path.stem not in run.result_ids:
                    issues.append(f"orphan result requires reconciliation: {path.stem}")
        for attempt in run.attempts:
            if not self.path(f"{attempt.directory}/intent.json").is_file():
                issues.append(f"missing immutable launch intent: {attempt.id}")
            if attempt.state in ("intent", "running", "unknown"):
                issues.append(f"attempt requires execution reconciliation: {attempt.id}")
        return issues
