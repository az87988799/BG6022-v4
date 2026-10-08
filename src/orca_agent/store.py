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
from typing import Any, Callable

from filelock import FileLock

from orca_agent._atomic import atomic_write
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
    ToolCall,
    fingerprint,
    new_id,
    utc_now,
)
from orca_agent.tools.registry import get_tool, validate_geometry, validate_parameters


class StoreError(ValueError):
    """A stored fact or requested operation violates a boundary."""


class BudgetExceeded(StoreError):
    """An immutable cumulative budget prevents a new launch."""


class ControlChanged(StoreError):
    """A new user/control fact must be absorbed before another action starts."""


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
        self, request: Request, plan: Plan | None,
        permission: PermissionSnapshot | None = None, budget: BudgetLimits | None = None,
        *, science_baseline_policy: str = "legacy",
    ) -> Run:
        request = Request.model_validate(request.model_dump())
        if plan is not None:
            plan = Plan.model_validate(plan.model_dump())
            plan.validate_request(request)
        geometry_ids = list(dict.fromkeys(filter(None, [request.geometry_artifact_id,
                       *(item.geometry_artifact_id for item in request.systems)])))
        for artifact_id in geometry_ids:
            if self.artifact_path(artifact_id).stat().st_size > 65536:
                raise StoreError("geometry exceeds the 64 KiB input limit")
        for step in plan.steps if plan else []:
            if "execute_orca" in get_tool(step.tool).effects and step.geometry.artifact_id:
                from orca_agent.applicability import validate_direct_geometry
                try:
                    validate_direct_geometry(self, request, step)
                except ValueError as exc:
                    raise StoreError(str(exc)) from exc
                initial = self.artifact_path(step.geometry.artifact_id)
                validate_geometry(initial.read_text(encoding="utf-8"), step.parameters)
        permission = permission or PermissionSnapshot(artifact_ids=geometry_ids)
        permission = PermissionSnapshot.model_validate(permission.model_dump())
        if not set(geometry_ids).issubset(permission.artifact_ids):
            raise StoreError("initial geometry is outside the permission snapshot")
        budget = budget or BudgetLimits()
        budget = BudgetLimits.model_validate(budget.model_dump())
        science_steps = ([s.logical_id for s in plan.steps
                          if "execute_orca" in get_tool(s.tool).effects] if plan else None)
        run = Run(
            request_id=request.id, request_version=request.version,
            plan_id=plan.id if plan else None, plan_version=plan.version if plan else None,
            permission=permission, budget=budget,
            deadline=utc_now() + timedelta(seconds=budget.run_seconds),
            goal_status={goal.id: "insufficient_evidence" for goal in request.goals},
            science_baseline_policy=science_baseline_policy,
            initial_science_steps=(science_steps or None
                                   if science_baseline_policy == "first_science_plan" else science_steps),
        )
        if plan:
            run.usage.logical_steps = [s.logical_id for s in plan.steps]
        self._write_json(
            f"runs/{run.id}/request-revisions/{request.version}.json", request, immutable=True
        )
        if plan:
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
        return self.load_request_revision(run, run.request_version)

    def _revision_payload(self, run: Run, kind: str, version: int) -> dict:
        """Resolve only a Run-activated decision, or the explicit initial legacy file.

        Candidate files do not allocate version numbers. Two unactivated proposals
        may legitimately contain the same next version after a control change.
        The atomic Run record, not directory order or file existence, selects one.
        """
        if type(version) is not int or version < 1:
            raise StoreError("revision version must be a positive integer")
        summaries = [item for item in run.decisions if item.get(f"{kind}_version") == version]
        selected = None
        for summary in summaries:
            identifier = _id(summary["id"])
            record = self._read_json(f"runs/{_id(run.id)}/decisions/{identifier}.json")
            if (record.get("id") != identifier or record.get("basis") != summary.get("basis")
                    or (summary.get("record_sha256")
                        and summary["record_sha256"] != fingerprint(record))):
                raise StoreError("activated decision identity or content changed")
            payload = record.get(kind)
            if (not isinstance(payload, dict) or payload.get("version") != version
                    or (kind == "plan" and summary.get("plan_id")
                        and payload.get("id") != summary["plan_id"])):
                raise StoreError("activated revision does not match its decision summary")
            if selected is not None and selected != payload:
                raise StoreError("activated decisions conflict on one immutable revision")
            selected = payload
        if selected is not None:
            return selected
        # Pre-revision runs have only their original immutable files. With decisions,
        # only the first decision's explicit prior version can name that old file.
        initial_version = (run.decisions[0].get("basis", {}).get(f"{kind}_version")
                           if run.decisions else getattr(run, f"{kind}_version"))
        if version != initial_version:
            raise StoreError("requested revision has not been activated")
        return self._read_json(f"runs/{_id(run.id)}/{kind}-revisions/{version}.json")

    def load_request_revision(self, run: Run | str, version: int) -> Request:
        run = self.load_run(run) if isinstance(run, str) else run
        request = Request.model_validate(self._revision_payload(run, "request", version))
        if (request.id, request.version) != (run.request_id, version):
            raise StoreError("request revision identity mismatch")
        return request

    def load_plan_revision(self, run: Run | str, version: int) -> Plan:
        run = self.load_run(run) if isinstance(run, str) else run
        plan = Plan.model_validate(self._revision_payload(run, "plan", version))
        known_ids = {item.get("plan_id") for item in run.decisions}
        known_ids.update(item.get("prior_plan_id") for item in run.decisions)
        known_ids.add(run.plan_id)
        known_ids.discard(None)
        if plan.version != version or (known_ids and known_ids != {plan.id}):
            raise StoreError("plan revision identity mismatch")
        plan.validate_request(self.load_request_revision(run, plan.request_version))
        return plan

    def load_plan(self, run: Run | str) -> Plan | None:
        run = self.load_run(run) if isinstance(run, str) else run
        if run.plan_id is None:
            return None
        plan = self.load_plan_revision(run, run.plan_version)
        if (plan.id, plan.version) != (run.plan_id, run.plan_version):
            raise StoreError("plan revision identity mismatch")
        if plan.request_version != run.request_version:
            raise StoreError("active Plan is bound to a different Request revision")
        return plan

    def save_run(self, run: Run) -> None:
        run = Run.model_validate(run.model_dump())
        with self.run_lock(run.id):
            previous = self.load_run(run.id)
            for field in ("request_id", "request_version", "plan_id", "plan_version", "permission",
                          "science_baseline_policy",
                          "budget", "created_at", "deadline"):
                if getattr(run, field) != getattr(previous, field):
                    raise StoreError(f"fixed run field cannot be changed: {field}")
            for field in ("orca_starts_reserved", "orca_starts_actual", "postprocess_starts",
                          "elapsed_seconds", "cpu_seconds", "extra_orca_starts_reserved",
                          "model_calls", "model_tokens_used", "model_tokens_unknown",
                          "plan_revisions", "decision_rounds",
                          "evidence_reads", "analysis_executions", "identity_queries", "structure_preparations", "knowledge_queries"):
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
                              "input_fingerprint", "directory", "created_at", "request_version",
                              "plan_version", "permission_version", "control_generation", "frozen_step", "consumption"):
                    if getattr(old, field) != getattr(new, field):
                        raise StoreError(f"attempt identity/input cannot be rewritten: {field}")
            if not set(previous.result_ids).issubset(run.result_ids):
                raise StoreError("result references cannot be removed")
            if previous.initial_science_steps != run.initial_science_steps:
                raise StoreError("initial scientific baseline requires controlled plan activation")
            for name in ("calls", "model_records", "decisions", "processed_messages", "processed_feedback", "applied_decisions"):
                if len(getattr(run, name)) < len(getattr(previous, name)):
                    raise StoreError("call/decision history cannot be removed")
            for name in ("decisions", "processed_messages", "processed_feedback", "applied_decisions"):
                if getattr(run, name)[:len(getattr(previous, name))] != getattr(previous, name):
                    raise StoreError("activated history cannot be rewritten")
            if not set(previous.usage.logical_steps).issubset(run.usage.logical_steps):
                raise StoreError("logical Step history cannot be reset")
            for old, new in zip(previous.calls, run.calls, strict=False):
                for field in ("id", "tool", "parameters", "step_id", "request_version", "plan_version",
                              "created_at", "control_generation", "frozen_step", "consumption"):
                    if getattr(old, field) != getattr(new, field):
                        raise StoreError("Tool call identity/input cannot be rewritten")
                if old.result_id and (old.result_id != new.result_id or old.state != new.state):
                    raise StoreError("settled Tool call cannot be rewritten")
            for old, new in zip(previous.model_records, run.model_records, strict=False):
                if old != new:
                    raise StoreError("model records require dedicated evidence-backed settlement")
            if run.input_bindings != previous.input_bindings:
                from orca_agent.input_bindings import validate_input_result
                request = self.load_request(run)
                for system_id, ports in run.input_bindings.items():
                    for port, binding in ports.items():
                        result = self.load_result(run.id, binding["result_id"])
                        actual = validate_input_result(self, run, request, result, port)
                        if actual != binding or actual["system_id"] != system_id:
                            raise StoreError("input binding must derive from exact current qualified evidence")
            self._write_json(f"runs/{run.id}/run.json", run)

    def settle_model(self, run: Run, ticket: str):
        """Atomically settle only a recorded reservation from its immutable receipt.

        Ordinary save_run cannot reduce unknown token occupancy or edit model
        records. This entry point reads and validates the request/receipt itself;
        caller-provided token numbers or proposed scientific outcomes have no say.
        """
        from orca_agent.model_usage import read_model_reply, settled_model_record

        with self.run_lock(run.id):
            if self.load_run(run.id) != run:
                raise StoreError("Run changed before model settlement")
            matches = [record for record in run.model_records if record.get("id") == ticket]
            if len(matches) != 1:
                raise StoreError("model settlement reservation identity mismatch")
            record = matches[0]
            reply, receipt_hash = read_model_reply(self, run, record)
            if record.get("status") != "reserved":
                if record.get("response_record_sha256") != receipt_hash:
                    raise StoreError("settled model response changed")
                return reply
            updated = run.model_copy(deep=True)
            index = run.model_records.index(record)
            updated.model_records[index] = settled_model_record(record, reply, receipt_hash)
            if reply.usage is not None:
                reserved = record["input_reserved"] + record["output_reserved"]
                if updated.usage.model_tokens_unknown < reserved:
                    raise StoreError("model reservation occupancy is inconsistent")
                updated.usage.model_tokens_unknown -= reserved
                updated.usage.model_tokens_used += reply.usage.total_tokens
            updated = Run.model_validate(updated.model_dump())
            self._write_json(f"runs/{run.id}/run.json", updated)
            run.__dict__.update(updated.__dict__)
            return reply

    def import_artifact(
        self, path: str | Path, role: str, *, run_id: str | None = None,
        attempt_id: str | None = None, source: dict | None = None,
        expected_sha256: str | None = None,
    ) -> Artifact:
        original = Path(path)
        if not original.is_file() or _is_link(original):
            raise StoreError("artifact source must be an existing regular, non-link file")
        artifact_id = new_id("artifact")
        relative = f"artifacts/{artifact_id}/files/{original.name}"
        destination = self.path(relative)
        before_hash = sha256_file(original)
        if expected_sha256 is not None and before_hash != expected_sha256:
            raise StoreError("artifact source differs from its authorized hash")
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

    def reserve_attempt(self, run: Run, step: Step, geometry_artifact_id: str, *,
                        before_reserve: Callable[[Attempt], None] | None = None) -> Attempt:
        """Validate before external accounting; lock order is Run, environment, callback.

        The callback may reserve an external budget but must not launch work.
        Its fixed Attempt identity precedes all intent/lease/Run publication.
        """
        with self.run_lock(run.id), self.control_lock(run.id), self._environment_lock():
            current = self.load_run(run.id)
            if current.model_dump() != run.model_dump():
                raise StoreError("stale run snapshot; reload and revalidate")
            if self.read_signal(run.id) in ("pause", "cancel"):
                raise StoreError("pause/cancel requested before submission")
            self.check_execution_control(run)
            if run.state not in ("ready", "running", "paused"):
                raise StoreError(f"run state does not permit a launch: {run.state}")
            plan = self.load_plan(run)
            if plan is None:
                raise StoreError("scientific execution requires a Plan")
            plan.validate_request(self.load_request(run))
            actual = next((item for item in plan.steps if item.id == step.id), None)
            if actual is None or actual != step:
                raise StoreError("step differs from its immutable plan revision")
            permission = PermissionSnapshot.model_validate(
                self._read_json(f"runs/{run.id}/permission.json")
            )
            if permission != run.permission or not permission.scientific_execution:
                raise StoreError("scientific execution is not authorized")
            if BudgetLimits.model_validate(self._read_json(
                    f"runs/{run.id}/budget.json")) != run.budget:
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
                    from orca_agent.input_bindings import prepared_geometry
                    if prepared_geometry(self, run, self.load_request(run), step.system_id) != geometry_artifact_id:
                        raise StoreError("geometry is not authorized")
            else:
                self._validate_future_binding(run, step, geometry_artifact_id)
            from orca_agent.applicability import validate_geometry_consumption
            try:
                validate_geometry_consumption(self, run, step, geometry_artifact_id)
            except ValueError as exc:
                raise StoreError(str(exc)) from exc
            for dependency in step.depends_on:
                if not any(a.step_id == dependency and a.result_id for a in run.attempts + run.calls):
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
            baseline = run.initial_science_steps
            extra = number > 1 or (baseline is not None and step.logical_id not in baseline)
            repeated = max(run.usage.extra_orca_starts_reserved,
                           sum(max(0, count - 1) for count in run.usage.logical_attempts.values()))
            if extra and repeated >= run.budget.extra_orca_starts:
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
                request_version=run.request_version, plan_version=run.plan_version,
                permission_version=run.permission.version, control_generation=run.control_generation,
                frozen_step=step.model_copy(deep=True),
                consumption={"geometry": {"artifact_id": geometry.id, "sha256": geometry.sha256}},
            )
            if self.path(attempt.directory).exists():
                raise StoreError("attempt directory already exists; reconcile its identity before reserving")
            if before_reserve:
                before_reserve(attempt)
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
            run.usage.extra_orca_starts_reserved = repeated + int(extra)
            run.usage.logical_attempts[step.logical_id] = number
            run.usage.fingerprint_attempts[input_fingerprint] = same_input_number
            run.state = "running"
            self.save_run(run)
            return attempt

    def recover_unstarted_reservation(self, run: Run, draft: Attempt, *, before: dict,
                                      reservation_sha256: str) -> Attempt:
        """Recover a recorded pre-publication reservation without inventing a launch.

        This requires the fixed draft supplied to reserve_attempt's callback.
        Missing handles alone are insufficient: exact counters, frozen input,
        intent, directory contents and lease must still prove the prelaunch boundary.
        The original reservation remains charged even when actual starts are zero.
        """
        with self.run_lock(run.id), self._environment_lock():
            current = self.load_run(run.id)
            for field in type(run).model_fields:
                setattr(run, field, getattr(current, field))
            matches = [a for a in run.attempts if a.id == draft.id]
            attempt = matches[0] if len(matches) == 1 else draft.model_copy(deep=True)
            plan = self.load_plan_revision(run, draft.plan_version)
            step = next((s for s in plan.steps if s.id == draft.step_id), None)
            geometry = self.load_artifact(draft.geometry_artifact_id)
            self.artifact_path(geometry.id)
            if (step != draft.frozen_step or not step or draft.state != "intent"
                    or draft.started or draft.execution_handle or draft.finished_at or draft.result_id
                    or draft.input_fingerprint != fingerprint({"tool": step.tool,
                        "parameters": step.parameters.model_dump(), "geometry_hash": geometry.sha256})
                    or draft.directory != f"runs/{run.id}/steps/{draft.step_id}/attempt-{draft.number:03d}"):
                raise StoreError("prelaunch reservation draft differs from its frozen inputs")
            lease = self.environment_lease()
            if lease and lease.get("run_id") == run.id:
                if (lease.get("attempt_id") != draft.id or lease.get("input_fingerprint") != draft.input_fingerprint
                        or lease.get("state") != "intent" or lease.get("execution_handle")):
                    raise StoreError("reservation has crossed or conflicts with the prelaunch boundary")
            directory = self.path(draft.directory)
            allowed = {"intent.json", "prelaunch-aborted.json", "execution.json"}
            if directory.exists() and any(p.name not in allowed or not p.is_file() for p in directory.iterdir()):
                raise StoreError("reservation contains possible startup or scientific evidence")
            original = {"run_id": run.id, "attempt": draft.model_dump(mode="json"),
                        "parameters": step.parameters.model_dump(), "request_version": draft.request_version,
                        "plan_version": draft.plan_version, "permission_version": draft.permission_version}
            intent = directory / "intent.json"
            if intent.exists() and self._read_json(f"{draft.directory}/intent.json") != original:
                raise StoreError("prelaunch reservation intent changed")
            proof_path = f"{draft.directory}/prelaunch-aborted.json"
            expected_proof = {"attempt_id": draft.id, "reservation_sha256": reservation_sha256,
                              "proof": "fixed reservation never crossed persisted execution preparation"}
            if self.path(proof_path).exists() and self._read_json(proof_path) != expected_proof:
                raise StoreError("prelaunch no-start proof changed")
            outcome = {"state": "failed", "reason": "reservation aborted before execution preparation",
                       "not_started": True, "handle": None, "resource_usage": {}, "exit_code": None,
                       "reservation_sha256": reservation_sha256}
            execution_path = f"{draft.directory}/execution.json"
            if self.path(execution_path).exists() and self._read_json(execution_path) != outcome:
                raise StoreError("existing execution receipt cannot be replaced by no-start proof")
            expected_count = before["orca_starts_reserved"] + int(bool(matches))
            if (run.usage.orca_starts_reserved != expected_count
                    or [a.id for a in run.attempts if a.id != draft.id] != before["attempt_ids"]
                    or run.usage.logical_attempts.get(draft.logical_id, 0) != draft.number - int(not matches)
                    or (matches and (attempt.execution_handle or attempt.started or attempt.result_id
                                     or attempt.state not in {"intent", "not_started"}))):
                raise StoreError("Run counters or Attempt crossed the prelaunch reservation boundary")
            if matches and attempt.finished_at:
                if not self.path(proof_path).exists() or not self.path(execution_path).exists():
                    raise StoreError("settled no-start reservation has no durable proof")
                return attempt
            self._write_json(f"{draft.directory}/intent.json", original, immutable=True)
            self._write_json(proof_path, expected_proof, immutable=True)
            self._write_json(execution_path, outcome, immutable=True)
            if not matches:
                run.attempts.append(attempt)
                run.usage.orca_starts_reserved += 1
                run.usage.logical_attempts[draft.logical_id] = draft.number
                run.usage.fingerprint_attempts[draft.input_fingerprint] = before["fingerprint_attempts"] + 1
                extra = draft.number > 1 or (run.initial_science_steps is not None
                                             and draft.logical_id not in run.initial_science_steps)
                repeated = max(before["extra_orca_starts_reserved"], sum(
                    max(0, count - 1) for key, count in run.usage.logical_attempts.items()
                    if key != draft.logical_id) + max(0, draft.number - 2))
                run.usage.extra_orca_starts_reserved = repeated + int(extra)
            if lease and lease.get("run_id") != run.id:
                # No lease was acquired for this reservation. Preserve another
                # Run's active job while settling this provably unstarted one.
                attempt.state, attempt.finished_at = "not_started", utc_now()
                self.save_run(run)
            else:
                self.finish_attempt(run, attempt.id, state="not_started", started=False, termination_confirmed=True)
            return attempt

    def _validate_future_binding(self, run: Run, step: Step, artifact_id: str) -> None:
        producer = step.geometry.producer_step_id
        if step.geometry.port == "prepared_geometry":
            from orca_agent.input_bindings import prepared_geometry
            request = self.load_request(run)
            if (prepared_geometry(self, run, request, step.system_id) != artifact_id
                    or run.input_bindings[step.system_id]["prepared_geometry"]["result_id"]
                    != run.selected_results.get(producer)):
                raise StoreError("future prepared geometry lacks its exact current input binding")
            return
        candidates = [a for a in run.attempts if a.step_id == producer and a.result_id]
        selected = run.selected_results.get(producer)
        if selected:
            candidates = [a for a in candidates if a.result_id == selected]
        if len(candidates) != 1:
            raise StoreError("producer requires exactly one explicit result selection")
        for attempt in candidates:
            result = self.load_result(run.id, attempt.result_id)
            output = result.qualified_outputs.get(step.geometry.port)
            if output and output.artifact_id == artifact_id:
                if result.attempt_id != attempt.id:
                    raise StoreError("producer result/attempt mismatch")
                required = get_tool(step.tool).required_input_checks.get(step.geometry.port)
                if not required or any(check.rule_version != required for check in output.checks):
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
        call = next((c for c in run.calls if c.id == result.call_id), None) if result.call_id else None
        if attempt is None and call is not None:
            if result.attempt_id is not None or result.step_id != call.step_id:
                raise StoreError("non-scientific result has an invalid call binding")
            if result.supersedes_result_id:
                raise StoreError("only unknown scientific recovery uses replacement results")
            if set(result.qualified_outputs) - set(get_tool(call.tool).output_ports):
                raise StoreError("call publishes an undeclared scientific port")
            definition = get_tool(call.tool)
            for port, output in result.qualified_outputs.items():
                if (output.checks != result.checks.get(port)
                        or any(c.rule_version != definition.check_version for c in output.checks)
                        or result.operation_status != "completed"):
                    raise StoreError("call output lacks its declared successful checks")
            for artifact_id in result.artifact_ids:
                self.artifact_path(artifact_id)
            self._write_json(f"runs/{run.id}/results/{result.id}.json", result, immutable=True)
            return
        if attempt is None or attempt.step_id != result.step_id:
            raise StoreError("result does not belong to a stored attempt")
        if result.supersedes_result_id:
            previous = self.load_result(run.id, result.supersedes_result_id)
            if (previous.attempt_id, previous.step_id) != (result.attempt_id, result.step_id):
                raise StoreError("replacement result refers to another attempt")
            if previous.id == result.id or previous.operation_status != "unknown":
                raise StoreError("only an earlier unknown result can be replaced during recovery")
            if (result.operation_status == "unknown" or result.qualified_outputs
                    or result.source.get("execution", {}).get("reconciliation", {}).get("state") != "terminated"):
                raise StoreError("recovery replacement requires termination evidence without scientific outputs")
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

    def reserve_call(self, run: Run, tool: str, parameters: dict, step: Step | None = None,
                     *, consumption: dict | None = None, validate_only: bool = False) -> ToolCall | None:
        """Reserve a non-scientific Tool without acquiring the scientific environment slot."""
        with self.run_lock(run.id), self.control_lock(run.id):
            if self.load_run(run.id) != run:
                raise StoreError("stale Run snapshot")
            self.check_execution_control(run)
            definition = get_tool(tool)
            if "execute_orca" in definition.effects:
                raise StoreError("scientific calls require a Plan/Step/Attempt")
            if tool not in run.permission.allowed_tools:
                raise StoreError("tool is not authorized")
            params = validate_parameters(tool, parameters).model_dump(mode="json")
            if definition.usage_counter:
                if run.state == "unknown" or any(c.state in {"reserved", "unknown"} for c in run.calls):
                    raise StoreError("unfinished input acquisition requires reconciliation before new execution")
                if any(c.tool == tool and c.request_version == run.request_version
                       and c.parameters == params for c in run.calls):
                    raise StoreError("input acquisition already has a durable Call; reuse or explicitly revise the request")
            writes = any(effect.startswith("write") or effect == "import_artifact"
                         for effect in definition.effects)
            if writes and (not run.permission.artifact_writes or step is None):
                raise StoreError("artifact writes require an authorized explicit Step")
            if step:
                plan = self.load_plan(run)
                if not plan or step not in plan.steps or step.tool != tool:
                    raise StoreError("call Step is not in the activated Plan")
                if params != step.parameters.model_dump(mode="json"):
                    raise StoreError("call parameters differ from the frozen Step")
            if self.read_signal(run.id) or utc_now() >= run.deadline:
                raise BudgetExceeded("call blocked by control/deadline")
            artifact_id = params.get("artifact_id")
            if artifact_id:
                own = {a for rid in run.result_ids for a in self.load_result(run.id, rid).artifact_ids}
                if artifact_id not in set(run.permission.artifact_ids) | own:
                    raise StoreError("artifact is outside the permission snapshot")
                self.artifact_path(artifact_id)
            if params.get("source_id") and params["source_id"] not in run.permission.source_ids:
                raise StoreError("external source is outside the permission snapshot")
            if params.get("run_id") and params["run_id"] != run.id:
                raise StoreError("cross-Run listing requires explicit imported evidence")
            if definition.preflight:
                import importlib
                module, name = definition.preflight.rsplit(".", 1)
                getattr(importlib.import_module(module), name)(self, run, params, tool, consumption)
            counter = definition.usage_counter or (
                "analysis_executions" if "write_analysis" in definition.effects
                or definition.output_ports else "evidence_reads")
            if getattr(run.usage, counter) >= getattr(run.budget, counter):
                raise BudgetExceeded(f"{counter} budget exhausted")
            if validate_only:
                return None
            call = ToolCall(tool=tool, parameters=params, step_id=step.id if step else None,
                            request_version=run.request_version, plan_version=run.plan_version,
                            control_generation=run.control_generation,
                            frozen_step=step.model_copy(deep=True) if step else None,
                            consumption=consumption or {})
            run.calls.append(call)
            setattr(run.usage, counter, getattr(run.usage, counter) + 1)
            if "prepare_geometry" in definition.effects:
                with self._environment_lock():
                    if self.environment_lease() is not None:
                        run.calls.pop()
                        setattr(run.usage, counter, getattr(run.usage, counter) - 1)
                        raise EnvironmentBusy("environment quota is occupied, including unknown preparation")
                    self.save_run(run)
                    lease = {"run_id": run.id, "attempt_id": call.id, "kind": "tool_call",
                             "data_root": str(self.root), "state": "intent",
                             "created_at": utc_now().isoformat()}
                    atomic_write(controlled_path(self.environment_root, "lease.json"), _json_bytes(lease))
            else:
                self.save_run(run)
            return call

    def finish_call(self, run: Run, call: ToolCall, result: Result) -> None:
        if result.call_id != call.id or result.run_id != run.id:
            raise StoreError("result call binding mismatch")
        self.save_result(result)
        call.state = ("unknown" if result.operation_status == "unknown" else
                      "completed" if result.operation_status == "completed" else "failed")
        call.result_id = result.id
        if result.id not in run.result_ids:
            run.result_ids.append(result.id)
        if call.step_id:
            run.selected_results[call.step_id] = result.id
        from orca_agent.input_bindings import bind_input_result
        bind_input_result(self, run, result)
        self.save_run(run)
        if "prepare_geometry" in get_tool(call.tool).effects:
            execution = result.source.get("execution", {})
            terminated = execution.get("state") in {"completed", "failed", "cancelled", "timed_out"}
            if result.source.get("not_started") or terminated:
                self.release_environment(run.id, call.id, termination_confirmed=True)

    def recover_calls(self, run: Run) -> bool:
        directory = self.path(f"runs/{run.id}/results")
        paths = list(directory.glob("*.json")) if directory.exists() else []
        if len(paths) > 128:
            raise StoreError("too many Results for bounded reconciliation")
        results = [self.load_result(run.id, path.stem) for path in paths]
        complete = True
        for call in run.calls:
            if call.state not in ("reserved", "unknown"):
                lease = self.environment_lease()
                if (lease and lease.get("kind") == "tool_call"
                        and (lease["run_id"], lease["attempt_id"]) == (run.id, call.id)
                        and call.result_id):
                    self.finish_call(run, call, self.load_result(run.id, call.result_id))
                continue
            candidates = [result for result in results if result.call_id == call.id]
            if len(candidates) > 1:
                raise StoreError("multiple unrelated Results for one Tool call")
            if not candidates:
                from orca_agent.tools.dispatch import recover_call_result
                recovered = recover_call_result(self, run, call)
                if recovered is not None:
                    self.save_result(recovered)
                    candidates = [recovered]
            if not candidates:
                call.state = "unknown"
                run.state = "unknown"
                self.save_run(run)
                complete = False
                continue
            self.finish_call(run, call, candidates[0])
            if call.state == "unknown":
                run.state = "unknown"
                self.save_run(run)
                complete = False
        return complete

    def commit_revision(self, run: Run, plan: Plan | None, *, decision_id: str,
                        basis: dict, request: Request | None = None,
                        user_message_ids: list[str] | None = None,
                        related_results: list[str] | None = None, fault=None,
                        semantic_record: dict | None = None) -> Run:
        """Validate, save immutable candidates, then atomically activate one revision."""
        from orca_agent.planning import validate_revision

        _id(decision_id)
        with self.run_lock(run.id), self.control_lock(run.id):
            current = self.load_run(run.id)
            if any(item["id"] == decision_id for item in current.decisions):
                return current
            if current != run:
                raise StoreError("stale Run at revision commit")
            expected = {"request_version": run.request_version, "plan_version": run.plan_version,
                        "permission_version": run.permission.version,
                        "control_generation": self.read_control(run.id)["generation"]}
            control_message = semantic_record is not None and semantic_record.get("kind") in {"pause", "cancel"}
            if basis != expected or (self.read_signal(run.id) and not control_message):
                raise StoreError("proposal basis is stale or control prevents activation")
            related_results = related_results or []
            if not set(related_results).issubset(run.result_ids):
                raise StoreError("decision references unbound feedback")
            prior_request, prior_plan = self.load_request(run), self.load_plan(run)
            messages = self.read_control(run.id)["messages"]
            if user_message_ids and not set(user_message_ids).issubset(m["id"] for m in messages):
                raise StoreError("Request revision lacks trusted user messages")
            next_request = request or prior_request
            if not user_message_ids and any(m["id"] not in run.processed_messages for m in messages):
                raise ControlChanged("pending user messages prevent Plan activation")
            if user_message_ids and set(user_message_ids) & set(run.processed_messages):
                raise StoreError("user message has already been consumed")
            if next_request != prior_request:
                added = next_request.messages[len(prior_request.messages):]
                trusted = {m["id"]: m for m in messages}
                if ({m.get("id") for m in added} != set(user_message_ids or [])
                        or any(m != trusted.get(m.get("id")) for m in added)):
                    raise StoreError("Request revision changed the authenticated user message")
            message_only = (semantic_record is not None and bool(user_message_ids)
                            and next_request == prior_request and plan == prior_plan)
            resolved_clarification = (semantic_record or {}).get("resolved_clarification_id")
            if resolved_clarification is not None:
                active = self.active_clarification(run)
                if (not active or resolved_clarification != active["id"] or not user_message_ids
                        or next_request.version <= prior_request.version
                        or semantic_record.get("kind") in {"continue", "status", "pause", "cancel"}):
                    raise StoreError("clarification resolution requires an authenticated new Request")
            if not message_only:
                validate_revision(prior_request, prior_plan, next_request, plan, run,
                                  user_update=bool(user_message_ids),
                                  authenticated_user_revision=semantic_record is not None)
            from orca_agent.applicability import validate_direct_geometry
            for step in plan.steps if plan else []:
                if "execute_orca" in get_tool(step.tool).effects and step.geometry.artifact_id:
                    try:
                        validate_direct_geometry(self, next_request, step, run=run)
                    except ValueError as exc:
                        raise StoreError(str(exc)) from exc
            # A Plan created with the Run first enters decision history as the
            # prior Plan of its suspension. Removing the current pointer cannot
            # turn its later replacement into an uncharged first Plan.
            had_plan = prior_plan is not None or any(
                d.get("plan_version") is not None or d.get("prior_plan_version") is not None
                for d in run.decisions)
            revising = plan is not None and had_plan and not message_only
            if revising and run.usage.plan_revisions >= run.budget.plan_revisions:
                raise BudgetExceeded("plan revision budget exhausted")
            logical = set(run.usage.logical_steps) | {
                s.logical_id for s in (plan.steps if plan else [])}
            if len(logical) > 12:
                raise BudgetExceeded("cumulative logical Step limit exhausted")
            record = {"id": decision_id, "basis": basis, "request": next_request.model_dump(mode="json"),
                      "plan": plan.model_dump(mode="json") if plan else None,
                      "related_results": related_results, "user_message_ids": user_message_ids or []}
            if semantic_record is not None:
                record["semantics"] = semantic_record
            self._write_json(f"runs/{run.id}/decisions/{decision_id}.json", record, immutable=True)
            if fault:
                fault("after_revision_saved")
            updated = run.model_copy(deep=True)
            updated.request_version = next_request.version
            updated.plan_id = plan.id if plan else None
            updated.plan_version = plan.version if plan else None
            science_steps = [s.logical_id for s in plan.steps
                             if "execute_orca" in get_tool(s.tool).effects] if plan else []
            if (plan and updated.initial_science_steps is None and
                    (science_steps or updated.science_baseline_policy == "legacy")):
                updated.initial_science_steps = science_steps
            if revising:
                updated.usage.plan_revisions += 1
            updated.usage.logical_steps = sorted(logical)
            updated.decisions.append({"id": decision_id, "basis": basis,
                                      "record_sha256": fingerprint(record),
                                      "prior_plan_id": prior_plan.id if prior_plan else None,
                                      "prior_plan_version": prior_plan.version if prior_plan else None,
                                      "plan_id": updated.plan_id,
                                      "plan_version": updated.plan_version,
                                      "request_version": updated.request_version,
                                      "user_message_ids": user_message_ids or []})
            if semantic_record is not None:
                updated.decisions[-1]["semantics"] = semantic_record
            updated.processed_feedback = list(dict.fromkeys(updated.processed_feedback + related_results))
            updated.processed_messages = list(dict.fromkeys(updated.processed_messages + (user_message_ids or [])))
            updated.control_generation = basis["control_generation"]
            updated.state = "ready" if plan else "waiting_user"
            if semantic_record:
                if semantic_record.get("kind") == "status":
                    updated.state = run.state
                elif semantic_record.get("kind") in {"pause", "cancel"}:
                    updated.state = "paused" if semantic_record["kind"] == "pause" else "cancelled"
            if next_request != prior_request:
                from orca_agent.goals import current_goal_evidence
                from orca_agent.models import EvidenceRef

                # Carry forward only explicitly selected evidence that still
                # answers the revised purpose. This also preserves unaffected
                # Plan outputs when the old Plan is suspended by a user update.
                # Results, Artifacts, attempts and usage remain historical facts.
                updated.goal_evidence = {}
                # These are current selections only; all Call/Result/Artifact
                # histories survive. Reacquisition needs a new validated Plan.
                updated.input_bindings = {}
                updated.goal_status = {goal.id: "insufficient_evidence" for goal in next_request.goals}
                for goal in next_request.goals:
                    selection = current_goal_evidence(self, run, next_request, goal, prior_plan)
                    if (selection["gaps"] or not selection["assessment"]
                            or selection["assessment"]["status"] != "passed"):
                        continue
                    result = selection["result"]
                    updated.goal_evidence[goal.id] = selection["binding"].evidence or EvidenceRef(
                        run_id=result.run_id, result_id=result.id, attempt_id=result.attempt_id,
                        port=goal.port, rule_version=goal.minimum_check_version)
                    updated.goal_status[goal.id] = "satisfied"
                updated.delivery_status = "pending"
            # All protected-field changes occur only here, after contract validation.
            self._write_json(f"runs/{run.id}/run.json", updated)
            if fault:
                fault("after_revision_activated")
            return updated

    def result_chain(self, run: Run, attempt: Attempt) -> list[Result]:
        """Select only one explicit, identity-consistent result chain, never by time."""
        directory = self.path(f"runs/{_id(run.id)}/results")
        candidates = {}
        if directory.exists():
            paths = list(directory.glob("*.json"))
            if len(paths) > 128:
                raise StoreError("result reconciliation exceeds its bounded record limit")
            for path in paths:
                result = self.load_result(run.id, path.stem)
                if result.attempt_id == attempt.id:
                    if result.step_id != attempt.step_id:
                        raise StoreError("result step identity mismatch")
                    candidates[result.id] = result
        if attempt.result_id and attempt.result_id not in candidates:
            raise StoreError("current result is missing from its attempt")
        if not candidates:
            return []
        children = {}
        roots = []
        for result in candidates.values():
            parent = result.supersedes_result_id
            if parent is None:
                roots.append(result.id)
                continue
            if parent not in candidates:
                raise StoreError("replacement parent is missing or belongs to another attempt")
            if parent in children:
                raise StoreError("result replacement chain has multiple terminal branches")
            previous = candidates[parent]
            if (previous.operation_status != "unknown" or result.operation_status == "unknown"
                    or result.qualified_outputs
                    or result.source.get("execution", {}).get("reconciliation", {}).get("state") != "terminated"):
                raise StoreError("invalid recovery result replacement relation")
            children[parent] = result.id
        if len(roots) != 1:
            raise StoreError("multiple unrelated results or cyclic replacement chain")
        chain = []
        current = roots[0]
        while current is not None:
            if current in {item.id for item in chain}:
                raise StoreError("cyclic result replacement chain")
            chain.append(candidates[current])
            current = children.get(current)
        if len(chain) != len(candidates):
            raise StoreError("disconnected result replacement chain")
        for result in chain:
            for artifact_id in result.artifact_ids:
                artifact = self.load_artifact(artifact_id)
                if (artifact.run_id, artifact.attempt_id) != (run.id, attempt.id):
                    raise StoreError("recovery artifact provenance mismatch")
                self.artifact_path(artifact_id)
        return chain

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
        if result_id:
            run.selected_results[attempt.step_id] = result_id
        self.save_run(run)
        if termination_confirmed:
            self.release_environment(run.id, attempt.id, termination_confirmed=True)

    def signal(self, run_id: str, action: str | None) -> None:
        self.load_run(run_id)
        if action not in (None, "pause", "cancel"):
            raise StoreError("unknown control signal")
        with self.control_lock(run_id):
            control = self.read_control(run_id)
            self._write_json(f"runs/{_id(run_id)}/control.json", {
                **control, "action": action, "generation": control["generation"] + 1,
                "created_at": utc_now().isoformat(),
            })

    def read_control(self, run_id: str) -> dict:
        relative = f"runs/{_id(run_id)}/control.json"
        data = self._read_json(relative) if self.path(relative).exists() else {}
        return {"action": None, "generation": 0, "messages": [], **data}

    def active_clarification(self, run: Run) -> dict | None:
        """Read the durable question until a grounded user revision resolves it."""
        active = None
        for decision in run.decisions:
            if decision.get("action") == "clarify":
                parameters = decision["parameters"]
                active = {"id": decision["id"], "basis": decision["basis"],
                          "questions": list(parameters["questions"]),
                          "unresolved": list(parameters["unresolved"])}
            elif active:
                semantics = decision.get("semantics", {})
                if (semantics.get("resolved_clarification_id") == active["id"]
                        and decision.get("user_message_ids")
                        and decision.get("request_version", 0) > active["basis"]["request_version"]
                        and semantics.get("kind") not in {"continue", "status", "pause", "cancel"}):
                    active = None
        return active

    def check_execution_control(self, run: Run) -> None:
        """Check under the short control lock; never acknowledge messages here."""
        with self.control_lock(run.id):
            control = self.read_control(run.id)
            if (control["generation"] != run.control_generation
                    or any(m["id"] not in run.processed_messages for m in control["messages"])):
                raise ControlChanged("unabsorbed user/control generation prevents a new Tool action")
            if run.state in {"paused", "cancelled"}:
                raise ControlChanged("paused or cancelled Run prevents a new Tool action")
            if self.active_clarification(run):
                raise ControlChanged("unanswered clarification prevents a new Tool action")
            if self.load_request(run).unresolved:
                raise ControlChanged("unresolved user conditions prevent a new Tool action")

    def enqueue_message(self, run_id: str, text: str, *, update: dict | None = None,
                        message_id: str | None = None) -> str:
        if not text.strip() or len(text.encode("utf-8")) > 8192:
            raise StoreError("user message must be nonempty and at most 8 KiB")
        with self.control_lock(run_id):
            run = self.load_run(run_id)
            control = self.read_control(run_id)
            if message_id is not None:
                _id(message_id)
                prior = next((m for m in control["messages"] if m["id"] == message_id), None)
                if prior:
                    if prior["text"] != text or prior.get("update") != update:
                        raise StoreError("message ID already names different content")
                    return message_id
            if sum(m["id"] not in run.processed_messages for m in control["messages"]) >= 24:
                raise BudgetExceeded("pending user message limit exhausted")
            if len(control["messages"]) >= 512:
                raise BudgetExceeded("retained user message history limit exhausted")
            message_id = message_id or new_id("message")
            control["messages"].append({"id": message_id, "text": text,
                                        "source": "user", "created_at": utc_now().isoformat()})
            control["messages"][-1]["request_version"] = run.request_version
            if update is not None:
                if not isinstance(update, dict) or len(_json_bytes(update)) > 65536:
                    raise StoreError("user update must be an object of at most 64 KiB")
                control["messages"][-1]["update"] = update
            control["generation"] += 1
            if len(_json_bytes(control)) > 8 * 1024 * 1024:
                raise BudgetExceeded("retained user message storage limit exhausted")
            self._write_json(f"runs/{run_id}/control.json", control)
            return message_id

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
        request = self.load_request(run)
        geometries = {request.geometry_artifact_id} | {
            item.geometry_artifact_id for item in request.systems}
        for geometry_id in geometries - {None}:
            try:
                self.artifact_path(geometry_id)
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
