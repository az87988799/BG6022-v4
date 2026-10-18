"""Task-code execution and a single bounded SDK call in the existing coordinator.

All task content is untrusted. The mailbox transports bytes, never a host path,
permission, command line, scientific success assertion or execution handle.
"""

from __future__ import annotations

import base64
import json
import os
import time
from contextlib import ExitStack
from pathlib import Path

from orca_agent.backends import local
from orca_agent.backends.isolation import AppContainerSession, clean_environment
from orca_agent.backends.task_files import collect_paths, read_mailbox
from orca_agent.models import (
    CodeRef,
    InputManifest,
    InputManifestEntry,
    NativeOrcaParameters,
    Result,
    fingerprint,
    utc_now,
)
from orca_agent.orca.native import prepare_native_input, read_pinned_input
from orca_agent.task_sdk import MAX_REQUEST_BYTES
from orca_agent.tools.calculation import _save_json
from orca_agent.tools.registry import get_tool


def _control(store, run, step):
    from orca_agent.models import BudgetLimits, PermissionSnapshot

    if store.load_run(run.id) != run or step not in store.load_plan(run).steps:
        raise ValueError("Run or Plan changed before managed execution")
    for name, model, expected in (("permission", PermissionSnapshot, run.permission),
                                   ("budget", BudgetLimits, run.budget)):
        snapshot = json.loads(store.path(f"runs/{run.id}/{name}.json").read_text("utf-8"))
        if model.model_validate(snapshot) != expected:
            raise ValueError(f"immutable {name} changed before managed execution")
    store.check_execution_control(run)
    if store.read_signal(run.id) or utc_now() >= run.deadline:
        raise ValueError("execution control/deadline prevents launch")


def launch_orca(store, run, step, config, prepared, workdir, root, *,
                on_started, job_name=None, parent_job_name=None, fault=None, deadline=None,
                boundary=None):
    """The sole ORCA process-launch path for OPI and native input adapters."""
    from orca_agent.natural import ensure_scientific_environment
    from orca_agent.store import sha256_file

    owned_boundary = boundary is None
    session = None
    try:
        resources = prepared["resources"]
        contract = get_tool("orca.run").check_contract
        if resources["cores"] > 1 and not contract["parallel_execution"]:
            raise ValueError("parallel_execution_unavailable: " + contract["parallel_execution_reason"])
        ensure_scientific_environment(store, config, run)
        environment = json.loads(store.path(f"runs/{run.id}/environment.json").read_text("utf-8"))
        if (str(config.orca_path) != environment["orca"]["path"]
                or sha256_file(config.orca_path) != environment["orca"]["sha256"]):
            raise ValueError("ORCA installation differs from the frozen environment")
        boundary = boundary or _prepare_native_boundary(config, root, workdir)
        session, staged = boundary["session"], boundary["staged"]
        if boundary["workdir"] != workdir:
            raise ValueError("native work directory differs from its pre-staged boundary")
        if staged["manifest"]["files"]["orca.exe"]["sha256"] != environment["orca"]["sha256"]:
            raise ValueError("staged ORCA differs from the frozen executable")
        if resources["cores"] > 1:
            mpi = staged["manifest"]["files"].get("mpiexec.exe")
            if (mpi is None or mpi["sha256"] != environment.get("mpi", {}).get("sha256")
                    or str(config.mpi_path) != environment.get("mpi", {}).get("path")):
                raise ValueError("parallel runtime differs from the frozen MPI executable")
        _save_json(root / "runtime-manifest.json", staged["manifest"])
        child_env = clean_environment(staged["executable"].parent, workdir)
        _control(store, run, step)
    except Exception as exc:
        if session and owned_boundary:
            session.close()
        return {"state": "failed", "reason": f"prelaunch_rejected: {exc}",
                "not_started": True, "handle": None, "exit_code": None, "resource_usage": {}}
    control_lock = store.control_lock(run.id)
    locked = False

    def hook(point):
        nonlocal locked
        if point == "after_resumed" and locked:
            control_lock.release()
            locked = False
        if fault:
            fault(point)

    def started(handle):
        on_started(handle)
        _control(store, run, step)

    try:
        control_lock.acquire()
        locked = True
        _control(store, run, step)
        outcome = local.run_managed(
            staged["executable"], [Path(prepared["input_path"]).name], workdir,
            cores=resources["cores"], total_memory_mb=resources["memory_mb"],
            timeout_s=min(resources["timeout_seconds"], (run.deadline - utc_now()).total_seconds(),
                          deadline - time.monotonic() if deadline is not None else float("inf")),
            on_started=started, job_name=job_name, parent_job_name=parent_job_name,
            argv0=os.path.relpath(staged["executable"], workdir),
            cancel_requested=lambda: store.read_signal(run.id) == "cancel",
            environment=child_env, isolation=session.policy(allow_children=True), fault=hook,
        )
    finally:
        if locked:
            control_lock.release()
        if owned_boundary:
            session.close()
    return outcome


def _prepare_native_boundary(config, root, workdir):
    """Prepare before task Python starts so expensive copies cannot stall its monitor."""
    from orca_agent.backends.runtime_files import stage_orca_runtime

    workdir.mkdir(parents=True, exist_ok=True)
    staged = stage_orca_runtime(root / "runtime", config.orca_path, mpi_path=config.mpi_path)
    session = AppContainerSession(root)
    try:
        session.grant_read(root / "runtime")
        session.grant_write(workdir)
    except Exception:
        session.close()
        raise
    return {"staged": staged, "session": session, "workdir": workdir}


def _archive(store, run, attempt, folder, prefix, artifacts=None, *, checkpoint=None):
    artifacts = {} if artifacts is None else artifacts
    start = time.monotonic()
    for path in collect_paths(folder, count=run.budget.artifact_count):
        if checkpoint:
            checkpoint()
        if time.monotonic() - start > store.run_configuration(run).resources.collection_timeout_seconds:
            raise TimeoutError("task artifact collection deadline exhausted")
        name = prefix + path.relative_to(folder).as_posix()
        artifact = store.import_artifact(path, "raw_evidence", run_id=run.id,
                                         attempt_id=attempt.id, source={"task_path": name},
                                         checkpoint=checkpoint)
        artifacts[name] = {"artifact_id": artifact.id, "sha256": artifact.sha256}
    return artifacts


def collect_result(store, run, step, attempt, outcome):
    """Keep actual files; no fixed JSON requirement or implicit scientific parser."""
    root = store.path(attempt.directory)
    files, diagnostics = {}, []
    if outcome["state"] != "unknown":
        try:
            for folder in ("sealed", "task"):
                if (root / folder).exists():
                    _archive(store, run, attempt, root / folder, folder + "/", files)
            # Coordinator records are outside every task-writable ACL tree.
            for path in root.glob("*.json"):
                artifact = store.import_artifact(path, "execution_record", run_id=run.id,
                                                 attempt_id=attempt.id)
                files[path.name] = {"artifact_id": artifact.id, "sha256": artifact.sha256}
            for receipt in run.internal_calls:
                if receipt.parent_attempt_id == attempt.id:
                    internal = root / "internal" / receipt.request_key
                    raw_task = root / "native" / "task"
                    if not raw_task.exists():
                        raw_task = internal / "task"
                    if not receipt.artifact_ids and raw_task.is_dir():
                        _archive(store, run, attempt, raw_task,
                                 f"internal/{receipt.request_key}/task/", files)
                        for path in internal.glob("*.json"):
                            artifact = store.import_artifact(path, "execution_record", run_id=run.id,
                                                             attempt_id=attempt.id)
                            files[f"internal/{receipt.request_key}/{path.name}"] = {
                                "artifact_id": artifact.id, "sha256": artifact.sha256}
                    for artifact_id in receipt.artifact_ids:
                        artifact = store.load_artifact(artifact_id)
                        files[f"internal/{receipt.request_key}/{artifact_id}"] = {
                            "artifact_id": artifact_id, "sha256": artifact.sha256}
        except Exception as exc:
            diagnostics.append({"category": "collection_error", "detail": str(exc)})
    else:
        diagnostics.append({"category": "execution_unknown", "detail": outcome.get("reason")})
    if outcome["state"] != "completed":
        diagnostics.append({"category": outcome["state"], "detail": outcome.get("reason")})
    reference = getattr(step.parameters, "code", None) or getattr(step.parameters, "input", None)
    return Result(run_id=run.id, step_id=step.id, attempt_id=attempt.id,
                  operation_status=outcome["state"], artifact_ids=[v["artifact_id"] for v in files.values()],
                  diagnostics=diagnostics,
                  observations={"execution_completed": outcome["state"] == "completed",
                                "evidence_collection_complete": not any(
                                    item["category"] in {"collection_error", "execution_unknown"}
                                    for item in diagnostics),
                                "scientific_validation": "unverified"},
                  source={"files": files, "execution": outcome,
                          "code_sha256": reference.sha256 if reference else None,
                          "request_version": attempt.request_version,
                          "plan_version": attempt.plan_version,
                          "permission_version": attempt.permission_version})


class _Broker:
    def __init__(self, store, run, step, attempt, config, session, requests, responses, deadline,
                 native_boundary=None):
        self.store, self.run, self.step, self.attempt = store, run, step, attempt
        self.config, self.session = config, session
        self.requests, self.responses = requests, responses
        self.handled = False
        self.deadline = deadline
        self.native_boundary = native_boundary

    def poll(self, parent_handle):
        target = self.requests / "request.json"
        if self.handled or not target.exists():
            return
        self.handled = True
        try:
            data = read_mailbox(target, MAX_REQUEST_BYTES)
            response = self.execute(json.loads(data), parent_handle)
        except Exception as exc:
            response = {"error": f"managed_orca_request_rejected: {exc}"}
        _save_json(self.responses / "response.json", response)

    def execute(self, value, parent_handle):
        if self.native_boundary is None:
            raise ValueError("ORCA SDK boundary was not authorized/prepared before task execution")
        if time.monotonic() >= self.deadline:
            raise TimeoutError("script deadline exhausted before SDK admission")
        if (not isinstance(value, dict) or set(value) != {"version", "request_key", "input", "dependencies"}
                or type(value["version"]) is not int or value["version"] != 1
                or not isinstance(value["input"], str) or not isinstance(value["dependencies"], list)
                or len(value["dependencies"]) > 32):
            raise ValueError("invalid bounded SDK request")
        # Validate names and hashes with the same native-input schema. No child path is opened.
        from orca_agent.models import InternalCall
        InternalCall(request_key=value["request_key"], input_fingerprint=fingerprint(value), state="reserved")

        def checkpoint():
            # The task is still alive while its coordinator imports/copies evidence.
            # Keep the same parent's cancellation, memory and deadline monitor active.
            local.check_active_job(parent_handle["job_name"])

        checkpoint()
        root = self.store.path(self.attempt.directory)
        sealed = root / "sealed" / "sdk"
        sealed.mkdir()
        source = sealed / "native.inp"
        source.write_text(value["input"], encoding="utf-8")
        code = self.store.import_artifact(source, "native_input", run_id=self.run.id,
                                          attempt_id=self.attempt.id, checkpoint=checkpoint)
        entries = []
        for index, dependency in enumerate(value["dependencies"]):
            if not isinstance(dependency, dict) or set(dependency) != {"target_path", "content_base64"}:
                raise ValueError("invalid dependency transport")
            content = base64.b64decode(dependency["content_base64"], validate=True)
            dep = sealed / f"dependency-{index}"
            dep.write_bytes(content)
            artifact = self.store.import_artifact(dep, "native_dependency", run_id=self.run.id,
                                                  attempt_id=self.attempt.id, checkpoint=checkpoint)
            entries.append(InputManifestEntry(artifact_id=artifact.id, sha256=artifact.sha256,
                                               target_path=dependency["target_path"], purpose="native_dependency"))
        parameters = NativeOrcaParameters(input=CodeRef(artifact_id=code.id, sha256=code.sha256,
                                                        language="orca"),
                                          inputs=InputManifest(entries=entries))
        # Admission precedes ORCA reservation; failed source preparations remain recorded.
        prepare_root = self.native_boundary["workdir"]
        prepared = prepare_native_input(prepare_root, parameters, self.store.artifact_path,
                                         self.store.run_configuration(self.run).resources)
        _control(self.store, self.run, self.step)
        receipt = self.store.reserve_internal_call(
            self.run, self.attempt, value["request_key"], fingerprint(value),
            code=parameters.input, inputs=parameters.inputs,
            work_units=prepared["execution_impact"]["internal_work_units"])
        if receipt.state == "completed":
            raise ValueError("completed SDK receipt already exists; task replay is not enabled")
        internal_root = root / "internal" / receipt.request_key
        task = prepare_root
        prepared["input_path"] = str(task / "job.inp")
        _save_json(internal_root / "prepared.json", prepared)

        def started(handle):
            self.store.mark_internal_call_started(self.run, self.attempt, receipt.request_key, handle)

        outcome = launch_orca(self.store, self.run, self.step, self.config, prepared, task,
                              internal_root, on_started=started,
                              parent_job_name=parent_handle["job_name"], deadline=self.deadline,
                              boundary=self.native_boundary)
        _save_json(internal_root / "execution.json", outcome)
        files = {}
        collection_error = None
        try:
            if outcome["state"] != "unknown":
                _archive(self.store, self.run, self.attempt, task, "", files, checkpoint=checkpoint)
                for name in ("intent.json", "started.json", "prepared.json", "execution.json", "runtime-manifest.json"):
                    path = internal_root / name
                    if path.exists():
                        artifact = self.store.import_artifact(path, "execution_record", run_id=self.run.id,
                                                              attempt_id=self.attempt.id,
                                                              checkpoint=checkpoint)
                        files["record/" + name] = {"artifact_id": artifact.id, "sha256": artifact.sha256}
        except Exception as exc:
            collection_error = str(exc)
        self.store.finish_internal_call(self.run, self.attempt, receipt.request_key,
            state="failed" if collection_error else outcome["state"],
            artifact_ids=[item["artifact_id"] for item in files.values()],
            actual_conditions={**prepared["actual_conditions"], "collection_error": collection_error},
            resource_usage=outcome["resource_usage"])
        if collection_error:
            raise ValueError(f"ORCA evidence collection incomplete: {collection_error}")
        if outcome["state"] == "unknown":
            raise ValueError("internal ORCA termination is unknown; no raw files exposed")
        # The task receives new read-only copies, never the original evidence store path.
        output = self.responses / "files"
        output.mkdir()
        for name in files:
            if name.startswith("record/"):
                continue
            checkpoint()
            target = output / name
            target.parent.mkdir(parents=True, exist_ok=True)
            with (task / name).open("rb") as reader, target.open("xb") as writer:
                while block := reader.read(1024 * 1024):
                    checkpoint()
                    writer.write(block)
            checkpoint()
        self.session.grant_read(output)
        checkpoint()
        return {"request_key": receipt.request_key, "input_fingerprint": receipt.input_fingerprint,
                "operation_status": outcome["state"],
                "scientific_validation": "unverified", "actual_conditions": prepared["actual_conditions"],
                "files": {name: str(output / name) for name in files if not name.startswith("record/")}}


def execute(store, run, step, attempt, config, fault=None):
    """Execute generic Tools under LPAC; failure never falls back to host execution."""
    from orca_agent.backends.runtime_files import stage_python_runtime

    root = store.path(attempt.directory)
    task = root / "task"
    task.mkdir()
    resources = store.run_configuration(run).resources
    invoked = False
    stack = ExitStack()

    def started(handle):
        attempt.execution_handle = handle
        attempt.state = "running"
        _save_json(root / "started.json", handle)
        store.update_lease_handle(run.id, attempt.id, handle)
        store.save_run(run)
        _control(store, run, step)

    try:
        _control(store, run, step)
        if "execute_python" not in get_tool(step.tool).effects:
            prepared = prepare_native_input(task, step.parameters, store.artifact_path, resources)
            _save_json(root / "prepared.json", prepared)
            invoked = True
            outcome = launch_orca(store, run, step, config, prepared, task, root,
                                  on_started=started, job_name=attempt.execution_handle["job_name"], fault=fault)
        else:
            if len(step.parameters.inputs.entries) > run.budget.artifact_count:
                raise ValueError("task input count exceeds the frozen artifact budget")
            input_bytes = sum(store.load_artifact(entry.artifact_id).size
                              for entry in step.parameters.inputs.entries)
            if input_bytes > run.budget.artifact_total_bytes:
                raise ValueError("task input copies exceed the frozen byte budget")
            native_boundary = None
            if (run.permission.scientific_execution and "orca.run" in run.permission.allowed_tools
                    and config.orca_path is not None):
                native_boundary = _prepare_native_boundary(config, root / "native", root / "native" / "task")
                stack.callback(native_boundary["session"].close)
                _save_json(root / "native-runtime-manifest.json", native_boundary["staged"]["manifest"])
            sealed = root / "sealed"
            sealed.mkdir()
            code = read_pinned_input(store.artifact_path(step.parameters.code.artifact_id),
                                     step.parameters.code.sha256, limit=MAX_REQUEST_BYTES)
            (sealed / "task.py").write_bytes(code)
            _save_json(sealed / "inputs.json", step.parameters.model_dump(mode="json"))
            for entry in step.parameters.inputs.entries:
                data = read_pinned_input(store.artifact_path(entry.artifact_id), entry.sha256,
                                         limit=run.budget.artifact_bytes)
                target = task / entry.target_path
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(data)
            staged = stage_python_runtime(root / "runtime", Path(__file__).parents[1] / "task_sdk.py")
            _save_json(root / "runtime-manifest.json", staged["manifest"])
            requests, responses = root / "requests", root / "responses"
            requests.mkdir()
            responses.mkdir()
            with AppContainerSession(root) as session:
                for path in (root / "runtime", sealed, responses):
                    session.grant_read(path)
                for path in (task, requests):
                    session.grant_write(path)
                environment = clean_environment(root / "runtime", task)
                timeout = min(resources.script_timeout_seconds, (run.deadline - utc_now()).total_seconds())
                environment.update(ORCA_TASK_REQUESTS=str(requests), ORCA_TASK_RESPONSES=str(responses),
                                   ORCA_TASK_TIMEOUT_SECONDS=str(timeout))
                broker = _Broker(store, run, step, attempt, config, session, requests, responses,
                                 time.monotonic() + timeout, native_boundary)
                lock = store.control_lock(run.id)
                locked = False

                def hook(point):
                    nonlocal locked
                    if point == "after_resumed" and locked:
                        lock.release()
                        locked = False
                    if fault:
                        fault(point)

                try:
                    lock.acquire()
                    locked = True
                    _control(store, run, step)
                    invoked = True
                    outcome = local.run_managed(staged["executable"], ["-I", str(sealed / "task.py")], task,
                        cores=resources.max_cores, total_memory_mb=resources.total_memory_mb,
                        timeout_s=timeout, environment=environment, isolation=session.policy(),
                        on_started=started, on_poll=broker.poll, job_name=attempt.execution_handle["job_name"],
                        cancel_requested=lambda: store.read_signal(run.id) == "cancel", fault=hook)
                finally:
                    if locked:
                        lock.release()
    except Exception as exc:
        # No retry on a lost backend receipt. Reconciliation owns all uncertainty.
        outcome = {"state": "unknown" if invoked else "failed", "reason": str(exc),
                   "not_started": not invoked, "handle": attempt.execution_handle if invoked else None,
                   "exit_code": None, "resource_usage": {}}
    finally:
        stack.close()
    _save_json(root / "execution.json", outcome)
    if outcome["state"] != "unknown":
        for receipt in run.internal_calls:
            if receipt.parent_attempt_id == attempt.id and receipt.state in {"reserved", "running", "unknown"}:
                # The outer Job contains every internal process; only its confirmed
                # termination permits conservative settlement of an interrupted broker.
                store.finish_internal_call(run, attempt, receipt.request_key, state="failed",
                    actual_conditions={"termination_basis": "parent_job_terminated",
                                       "operation_details": "incomplete_broker_receipt"},
                    resource_usage={"complete": False})
    return collect_result(store, run, step, attempt, outcome), outcome
