"""Read-only registered native evidence discovery, shared by local raw audits.

No product imports, directory discovery, task execution, or scientific parsing.
The caller supplies a settled Run and retain(path), which hashes each read source.
Missing evidence remains unverified; contradictory identity/hash evidence raises.
Canonical file names are a view only: every reference preserves its original key.
"""
import hashlib
import json
import re
from pathlib import Path, PurePosixPath


def _relative(value):
    assert isinstance(value, str) and value and "\\" not in value and ":" not in value
    path = PurePosixPath(value)
    assert not path.is_absolute() and all(part not in {"", ".", ".."} for part in value.split("/"))
    return value


def _sha(raw):
    return hashlib.sha256(raw).hexdigest()


def _launch_proof(run, parent, receipt, scope, execution, actual, issues):
    """Authenticate the Linux trusted-launch receipt; never infer exec from stdout."""
    def require(ok, message):
        if not ok:
            issues.append(message)
    require(receipt.get("started") is True, "receipt does not confirm actual start")
    require(bool(execution) and execution.get("actual_start_status") == "confirmed"
            and execution.get("not_started") is False, "execution record does not confirm actual start")
    handle = receipt.get("execution_handle") or {}
    require(handle.get("backend") == "linux-container", "this audit has no verified launch-proof adapter for this backend")
    launch = execution.get("launch_receipt") or {}
    proof = handle.get("actual_start_evidence") or {}
    require(bool(actual), "archived actual-start record missing")
    require(bool(proof) and proof == launch.get("actual_start_evidence")
            == (execution.get("handle") or {}).get("actual_start_evidence")
            == actual.get("actual_start_evidence"), "actual-start proof differs across trusted records")
    require(launch.get("version") == 1 and launch.get("kind") == "orca"
            and launch.get("status") == "confirmed", "trusted launch receipt is not confirmed ORCA")
    require(proof.get("method") in {"proc_exe_inode_and_pid_start_ticks", "trusted_subprocess_exec_error_pipe"}
            and proof.get("executable") == "/opt/orca/orca"
            and type(proof.get("device")) is int and proof["device"] >= 0
            and type(proof.get("inode")) is int and proof["inode"] > 0
            and type(proof.get("pid")) is int and proof["pid"] > 0
            and bool(re.fullmatch(r"[0-9]+", str(proof.get("start_ticks", "")))),
            "actual executable identity proof missing or unsupported")
    if proof.get("method") == "trusted_subprocess_exec_error_pipe":
        require(proof.get("exec_error_pipe") == "closed_without_error"
                and type(proof.get("child_exit_code")) is int and 0 <= proof["child_exit_code"] <= 255,
                "trusted subprocess exec proof is incomplete")
    for key in ("pid", "start_ticks"):
        require(proof.get(key) == (launch.get("identity") or {}).get(key)
                == (handle.get("workload_identity") or {}).get(key), "launch identity mismatch: " + key)
    launch_hash = _sha((json.dumps(launch, separators=(",", ":")) + "\n").encode())
    require(launch_hash == handle.get("launch_receipt_sha256")
            == (execution.get("handle") or {}).get("launch_receipt_sha256")
            == actual.get("launch_receipt_sha256"), "launch receipt serialization hash mismatch")
    configuration = run.get("configuration", {}).get("execution", {})
    for field, value in {"run_id": run["id"], "attempt_id": parent["id"], "kind": "orca",
                         "orca_scope": scope, "image_id": configuration.get("image_id"),
                         "manifest_sha256": configuration.get("manifest_sha256")}.items():
        require(value is not None or field == "orca_scope", "frozen launch identity missing: " + field)
        require(all(item.get(field) == value for item in (handle, execution.get("handle") or {}, actual)),
                "trusted launch identity mismatch: " + field)
    for field in ("launch_id", "container_id"):
        require(bool(handle.get(field)) and handle.get(field) == actual.get(field)
                == (execution.get("handle") or {}).get(field), "launch handle identity mismatch: " + field)
    require(all(item.get("launch_phase") == "executed" for item in
                (handle, execution.get("handle") or {}, actual)), "trusted handle is not in executed phase")


def discover_native_sources(data, run, retain):
    """Return one row per registered direct Attempt or internal Call, never per replay.

    Each row has result/parent/receipt, native_identity, state/started/completed,
    source_authenticated, actual_started_confirmed, verification_issues, execution,
    files: canonical name -> (reference, bytes), and source_record_refs.
    Caller must require both authentication booleans for a scientific raw audit.
    A failed outer Python Result does not change its settled internal Call status.
    """
    data = Path(data).resolve()
    directory = data / "runs" / _relative(run["id"])
    run_path = directory / "run.json"
    run_reference = retain(run_path)
    assert json.loads(run_path.read_bytes()) == run, "Run is not the exact supplied saved snapshot"
    assert not any(item["state"] in {"intent", "reserved", "running", "unknown"}
                   for field in ("attempts", "calls", "internal_calls") for item in run.get(field, [])), "Run has unsettled execution"
    attempts = {item["id"]: item for item in run["attempts"]}
    assert len(attempts) == len(run["attempts"])
    receipts = [(item, item, None) for item in run["attempts"] if item.get("execution_kind") == "orca"]
    keys = set()
    for call in run.get("internal_calls", []):
        assert call["request_key"] not in keys, "Duplicate internal request key"
        keys.add(call["request_key"])
        _relative(call["request_key"])
        assert "/" not in call["request_key"]
        receipts.append((attempts[call["parent_attempt_id"]], call, call["request_key"]))
    rows = []
    for parent, receipt, scope in receipts:
        issues, start_issues, canonical, original = [], [], {}, {}
        refs = {"run": run_reference}
        result_id = parent.get("result_id")
        assert result_id in run["result_ids"], "Native producer Result is not registered"
        result_path = directory / "results" / (_relative(result_id) + ".json")
        refs["result"] = retain(result_path)
        result = json.loads(result_path.read_bytes())
        assert result["id"] == result_id and result["run_id"] == run["id"] and result["attempt_id"] == parent["id"]
        assert result["operation_status"] == parent["state"]
        source = result.get("source", {})
        for field in ("request_version", "plan_version", "permission_version"):
            if source.get(field) != parent.get(field):
                issues.append("Result differs from frozen parent " + field)
        consumed = parent.get("consumption", {})
        outer_code = consumed.get("code") or parent.get("frozen_step", {}).get("parameters", {}).get("input", {})
        if not outer_code or source.get("code_sha256") != outer_code.get("sha256"):
            issues.append("Result code hash differs from frozen parent")
        def artifact(artifact_id, expected, key, *, owner, member=True):
            _relative(artifact_id)
            if member:
                assert artifact_id in result["artifact_ids"]
            metadata_path = data / "artifacts" / artifact_id / "artifact.json"
            metadata_ref = retain(metadata_path)
            metadata = json.loads(metadata_path.read_bytes())
            assert metadata["id"] == artifact_id and metadata["run_id"] == run["id"]
            if owner is not None:
                assert metadata.get("attempt_id") == owner
            path = (data / _relative(metadata["path"])).resolve()
            assert path.is_relative_to(data)
            file_ref = retain(path)
            raw = path.read_bytes()
            assert file_ref["sha256"] == _sha(raw) == metadata["sha256"] == expected
            assert len(raw) == metadata["size"]
            return ({"result_id": result_id, "attempt_id": parent["id"], "artifact_id": artifact_id,
                     "source_file_key": key, **file_ref, "metadata": metadata_ref,
                     "metadata_source": metadata.get("source", {}), "owner_and_hash_verified": True}, raw, metadata)
        bindings = source.get("files", {})
        chosen = {}
        owned = set(receipt.get("artifact_ids", [])) if scope else None
        prefix = f"internal/{scope}/" if scope else ""
        for key, binding in bindings.items():
            if scope:
                if not key.startswith(prefix):
                    continue
                assert binding["artifact_id"] in owned
            elif key.startswith("internal/"):
                continue
            assert binding["artifact_id"] not in chosen, "Artifact has ambiguous Result aliases"
            chosen[binding["artifact_id"]] = artifact(binding["artifact_id"], binding["sha256"], key, owner=parent["id"])
            original[key] = chosen[binding["artifact_id"]][:2]
        if scope and set(chosen) != owned:
            issues.append("Some internal receipt artifacts are missing from the parent Result files")
        parent_dir = (data / _relative(parent["directory"])).resolve()
        assert parent_dir.is_relative_to(directory)
        record_dir = parent_dir / "internal" / scope if scope else parent_dir
        # Management names are the fixed archive contract, matched to their full
        # recorded import path, not searched by basename across any directory.
        expected_records = {(record_dir / name).resolve(): name for name in
                            ("intent.json", "started.json", "actual-start.json", "prepared.json", "execution.json", "runtime-manifest.json")}
        response = receipt.get("response") or {} if scope else {}
        response_files = response.get("files", {})
        if scope:
            assert parent.get("execution_kind") == "python"
            if (response.get("request_key") != scope or response.get("input_fingerprint") != receipt["input_fingerprint"]
                    or response.get("operation_status") != receipt["state"]):
                issues.append("Pinned internal response does not match the settled receipt")
            for name, binding in response_files.items():
                _relative(name)
                assert binding["artifact_id"] in owned
                item = chosen.get(binding["artifact_id"])
                if item is None:
                    issues.append("Pinned response file not archived in parent Result: " + name)
                    continue
                assert item[0]["sha256"] == binding["sha256"]
                assert item[2].get("source", {}).get("task_path") == name
                assert "task/" + name not in canonical
                canonical["task/" + name] = item[:2]
        for artifact_id, item in chosen.items():
            metadata_source = item[2].get("source", {})
            if scope and metadata_source.get("task_path") is not None:
                name = _relative(metadata_source["task_path"])
                if response_files.get(name, {}).get("artifact_id") != artifact_id:
                    issues.append("Internal task artifact is not pinned by the replay response: " + name)
                continue
            if not scope and item[0]["source_file_key"].startswith("task/"):
                name = item[0]["source_file_key"][5:]
                _relative(name)
                assert metadata_source.get("task_path") == "task/" + name
                canonical["task/" + name] = item[:2]
                continue
            imported = metadata_source.get("imported_from")
            if imported:
                record = expected_records.get(Path(imported).resolve())
                if record:
                    assert record not in canonical, "Ambiguous registered management record"
                    canonical[record] = item[:2]
        def record(name):
            return json.loads(canonical[name][1]) if name in canonical else {}
        intent, execution, actual = record("intent.json"), record("execution.json"), record("actual-start.json")
        frozen = intent.get("call" if scope else "attempt", {})
        if (intent.get("run_id") != run["id"] or frozen.get("input_fingerprint") != receipt["input_fingerprint"]
                or (scope and (intent.get("parent_attempt_id") != parent["id"] or frozen.get("request_key") != scope))
                or (not scope and frozen.get("id") != parent["id"])):
            issues.append("Archived native intent identity is missing or mismatched")
        parameters = {"input": receipt.get("code"), "inputs": receipt.get("inputs")} if scope else intent.get("parameters", {})
        code = parameters.get("input") or {}
        if scope and (frozen.get("code") != receipt.get("code") or frozen.get("inputs") != receipt.get("inputs")):
            issues.append("Internal frozen code/input manifest differs from receipt")
        if code and "task/source-input.inp" in canonical:
            artifact(code["artifact_id"], code["sha256"], "receipt.code" if scope else "intent.parameters.input",
                     owner=parent["id"] if scope else None, member=False)
            assert canonical["task/source-input.inp"][0]["sha256"] == code["sha256"]
        else:
            issues.append("Archived original native input binding missing")
        for entry in (parameters.get("inputs") or {}).get("entries", []):
            name = _relative(entry["target_path"])
            artifact(entry["artifact_id"], entry["sha256"], "native.inputs." + name,
                     owner=parent["id"] if scope else None, member=False)
            if "task/" + name not in canonical:
                issues.append("Archived native input dependency missing: " + name)
            else:
                assert canonical["task/" + name][0]["sha256"] == entry["sha256"]
        if execution.get("state") != receipt["state"]:
            issues.append("Archived execution state differs from receipt")
        if receipt["state"] == "completed" and execution.get("exit_code") != 0:
            issues.append("Completed native execution has no zero exit record")
        _launch_proof(run, parent, receipt, scope, execution, actual, start_issues)
        rows.append({"native_identity": {"run_id": run["id"], "parent_attempt_id": parent["id"],
                        "request_key": scope, "internal_attempt_id": receipt.get("attempt_id") if scope else None,
                        "input_fingerprint": receipt["input_fingerprint"]},
                     "result": result, "parent": parent, "receipt": receipt, "request_key": scope,
                     "state": receipt["state"], "started": receipt.get("started"),
                     "completed": receipt["state"] == "completed", "outer_operation_status": result["operation_status"],
                     "source_authenticated": not issues, "actual_started_confirmed": not start_issues,
                     "verification_issues": issues + start_issues, "files": canonical, "original_map": original,
                     "execution": execution, "source_record_refs": refs})
    actual_identities = set()
    for row in rows:
        if row["actual_started_confirmed"]:
            handle = row["receipt"]["execution_handle"]
            proof = handle["actual_start_evidence"]
            identity = (handle["container_id"], proof["pid"], proof["start_ticks"])
            assert identity not in actual_identities, "Distinct native receipts refer to the same actual process"
            actual_identities.add(identity)
    return rows
