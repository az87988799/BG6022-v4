"""Read-only product freeze for Code as Tools acceptance; never runs workloads.

This is an acceptance helper, not a product execution path. Runtime metadata
must be supplied by the caller's explicit environment inspection.
"""

from __future__ import annotations

import base64
import hashlib
import importlib
import importlib.metadata
import inspect
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from types import CodeType

SCHEMA_VERSION = 1
PRODUCT_COMMIT = "5e6e9e38c7e24a10180d474bf95068692b7f9cca"
SOURCE_ROOTS = ("src", "scripts/container_runtime")
FIXED_FILES = ("pyproject.toml", "uv.lock")
DEPENDENCIES = {
    "openai": "openai", "httpx": "httpx", "orca-pi": "opi",
    "pydantic": "pydantic", "filelock": "filelock", "psutil": "psutil",
    "fastapi": "fastapi", "uvicorn": "uvicorn", "rdkit": "rdkit", "numpy": "numpy",
}
_SECRET_KEYS = {"api_key", "authorization", "password", "secret", "credentials", "credential"}
MAX_DEPENDENCY_FILES = 100_000
MAX_DEPENDENCY_BYTES = 8 * 1024**3


class FreezeError(ValueError):
    """A frozen identity or its verification evidence is absent or changed."""


def _json(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True,
                      allow_nan=False).encode("utf-8")


def _sha(data):
    return hashlib.sha256(data).hexdigest()


def _safe_json(value):
    """Reject credential-bearing records instead of copying environment state."""
    def visit(item):
        if isinstance(item, dict):
            for key, child in item.items():
                if str(key).lower() in _SECRET_KEYS or str(key).lower().endswith("_api_key"):
                    raise FreezeError("credential field is not permitted in freeze evidence")
                visit(child)
        elif isinstance(item, (list, tuple)):
            for child in item:
                visit(child)
    visit(value)
    return json.loads(_json(value))


def _source_name(name):
    parts = Path(name).parts
    return ("__pycache__" not in parts and Path(name).suffix not in {".pyc", ".pyo"}
            and (name in FIXED_FILES or any(name.startswith(root + "/") for root in SOURCE_ROOTS)))


def product_files(project):
    """Include every source/resource, including newly added untracked files."""
    project = Path(project).resolve(strict=True)
    names = set(FIXED_FILES)
    for root in SOURCE_ROOTS:
        directory = project / root
        if (not directory.is_dir() or directory.is_symlink()
                or getattr(directory, "is_junction", lambda: False)()
                or not directory.resolve().is_relative_to(project)):
            raise FreezeError(f"product directory missing: {root}")
        for current, directories, files in os.walk(directory, followlinks=False):
            for name in [*directories, *files]:
                path = Path(current) / name
                if (path.is_symlink() or getattr(path, "is_junction", lambda: False)()
                        or not path.resolve().is_relative_to(project)):
                    raise FreezeError("product path redirects outside its ordinary source tree")
            directories[:] = [name for name in directories if name != "__pycache__"]
            for name in files:
                path = Path(current) / name
                relative = path.relative_to(project).as_posix()
                if _source_name(relative):
                    if not path.is_file():
                        raise FreezeError(f"product source is not a regular file: {relative}")
                    names.add(relative)
    for name in names:
        path = project / name
        if not path.is_file() or path.is_symlink() or not path.resolve().is_relative_to(project):
            raise FreezeError(f"product file missing or redirected: {name}")
    return sorted(names)


def _git(project, *args):
    """Only fixed, read-only Git plumbing; no shell or external diff/filter."""
    result = subprocess.run(["git", "--no-optional-locks", "-C", str(project), *args],
        check=False, capture_output=True, timeout=30, shell=False,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    if result.returncode:
        raise FreezeError("candidate Git objects could not be read")
    return result.stdout


def _commit_snapshot(project, code_commit):
    if not isinstance(code_commit, str) or re.fullmatch(r"[0-9a-fA-F]{7,40}", code_commit) is None:
        raise FreezeError("code_commit must be a concrete hexadecimal Git commit")
    commit = _git(project, "rev-parse", "--verify", code_commit + "^{commit}").decode().strip()
    if re.fullmatch(r"[0-9a-f]{40}", commit) is None:
        raise FreezeError("Git returned an invalid candidate commit")
    tree = _git(project, "ls-tree", "-r", "-z", commit, "--", *SOURCE_ROOTS, *FIXED_FILES)
    blobs = {}
    for row in tree.split(b"\0"):
        if not row:
            continue
        metadata, path = row.split(b"\t", 1)
        mode, kind, oid = metadata.decode("ascii").split()
        name = path.decode("utf-8")
        if not _source_name(name):
            continue
        if kind != "blob" or mode not in {"100644", "100755"}:
            raise FreezeError(f"candidate product path is not a regular blob: {name}")
        blobs[name] = _git(project, "cat-file", "blob", oid)
    return commit, blobs


def _matches_git_bytes(actual, committed):
    if actual == committed:
        return "exact"
    # Only UTF-8 text can use Git's CRLF checkout conversion. Binary resources
    # remain byte exact; the manifest also fixes the actual checkout hash.
    try:
        actual.decode("utf-8")
        committed.decode("utf-8")
    except UnicodeDecodeError:
        return None
    if b"\0" not in actual + committed and actual.replace(b"\r\n", b"\n") == committed.replace(b"\r\n", b"\n"):
        return "utf8-crlf-to-lf"
    return None


def source_snapshot(project, code_commit=PRODUCT_COMMIT):
    project = Path(project).resolve(strict=True)
    commit, blobs = _commit_snapshot(project, code_commit)
    names = product_files(project)
    if names != sorted(blobs):
        raise FreezeError("candidate product file set differs (added or missing source/resource)")
    files = {}
    for name in names:
        raw = (project / name).read_bytes()
        comparison = _matches_git_bytes(raw, blobs[name])
        if comparison is None:
            raise FreezeError(f"working product differs from candidate commit: {name}")
        files[name] = {"sha256": _sha(raw), "bytes": len(raw),
                       "commit_sha256": _sha(blobs[name]), "commit_comparison": comparison}
    return {"code_commit": commit, "files": files}


def _module_file(project, module):
    path = Path(getattr(module, "__file__", "")).resolve()
    source_root = (Path(project) / "src").resolve()
    if not path.is_file() or not path.is_relative_to(source_root):
        raise FreezeError(f"product import origin differs: {module.__name__}")
    return path.relative_to(Path(project).resolve()).as_posix()


def validate_product_imports(project, files):
    for name, module in tuple(sys.modules.items()):
        if name != "orca_agent" and not name.startswith("orca_agent."):
            continue
        if getattr(module, "__file__", None) is None:
            paths = list(getattr(module, "__path__", []))
            root = (Path(project) / "src/orca_agent").resolve()
            if paths and all(Path(path).resolve().is_relative_to(root) for path in paths):
                continue
            raise FreezeError(f"product import origin differs: {name}")
        if _module_file(project, module) not in files:
            raise FreezeError(f"imported product file absent from freeze: {name}")


def _code_identity(code):
    """Ignore source positions, but retain every execution-bearing code field."""
    def constant(value):
        if isinstance(value, CodeType):
            return {"code": _code_identity(value)}
        if isinstance(value, tuple):
            return {"tuple": [constant(item) for item in value]}
        if isinstance(value, frozenset):
            return {"frozenset": sorted((constant(item) for item in value), key=_json)}
        if isinstance(value, bytes):
            return {"bytes": value.hex()}
        if isinstance(value, float):
            return {"float": value.hex()}
        if isinstance(value, complex):
            return {"complex": [value.real.hex(), value.imag.hex()]}
        if value is Ellipsis:
            return {"ellipsis": True}
        if value is None or type(value) in {bool, int, str}:
            return {type(value).__name__: value}
        raise FreezeError("unsupported constant in registered implementation bytecode")

    fields = ("co_argcount", "co_posonlyargcount", "co_kwonlyargcount", "co_nlocals",
              "co_stacksize", "co_flags", "co_names", "co_varnames", "co_freevars",
              "co_cellvars", "co_name", "co_qualname")
    return {**{name: getattr(code, name) for name in fields},
            "co_code": code.co_code.hex(), "co_exceptiontable": code.co_exceptiontable.hex(),
            "co_consts": [constant(value) for value in code.co_consts]}


def _compiled_codes(code):
    yield code
    for value in code.co_consts:
        if isinstance(value, CodeType):
            yield from _compiled_codes(value)


def _callable_record(project, dotted):
    module_name, name = dotted.rsplit(".", 1)
    module = importlib.import_module(module_name)
    function = getattr(module, name)
    if not inspect.isfunction(function) or function.__module__ != module_name:
        raise FreezeError(f"registered implementation is not the expected function: {dotted}")
    if Path(function.__code__.co_filename).resolve() != Path(module.__file__).resolve():
        raise FreezeError(f"registered implementation code originates outside its module: {dotted}")
    compiled = compile(Path(module.__file__).read_bytes(), str(Path(module.__file__)),
                       "exec", dont_inherit=True, optimize=sys.flags.optimize)
    expected = [code for code in _compiled_codes(compiled)
                if code.co_qualname == function.__qualname__]
    identity = _code_identity(function.__code__)
    if len(expected) != 1 or identity != _code_identity(expected[0]):
        raise FreezeError(f"loaded implementation bytecode differs from frozen source: {dotted}")
    return {"name": dotted, "signature": str(inspect.signature(function)),
            "file": _module_file(project, module),
            "source_sha256": _sha(inspect.getsource(function).encode("utf-8")),
            "loaded_code_sha256": _sha(_json(identity))}


def product_contract(project):
    from orca_agent import context, llm, task_sdk
    from orca_agent.tools import knowledge, registry

    catalog = {}
    for name, registration in sorted(registry.TOOLS.items()):
        tool = registration.tool.model_dump(mode="json")
        functions = {key: _callable_record(project, tool[key])
                     for key in ("implementation", "preflight", "recovery") if tool.get(key)}
        if collector := tool.get("check_contract", {}).get("collector"):
            functions["collector"] = _callable_record(project, collector)
        catalog[name] = {"definition": tool, "functions": functions}
    sdk = {name: _callable_record(project, f"orca_agent.task_sdk.{name}")
           for name, value in vars(task_sdk).items()
           if not name.startswith("_") and inspect.isfunction(value)
           and value.__module__ == task_sdk.__name__}
    return _safe_json({
        "tools": catalog,
        "sdk": {"protocol_version": task_sdk.PROTOCOL_VERSION, "public_functions": sdk,
            "max_exchanges": task_sdk.MAX_EXCHANGES, "max_request_bytes": task_sdk.MAX_REQUEST_BYTES,
            "max_response_bytes": task_sdk.MAX_RESPONSE_BYTES},
        "prompt": {"version": context.PROMPT_VERSION, "model": llm.MODEL,
            "base_url": llm.BASE_URL, "token_bound_version": llm.TOKEN_BOUND_VERSION,
            "files": ["src/orca_agent/context.py", "src/orca_agent/readable_context.py",
                      "src/orca_agent/context_efficiency.py", "src/orca_agent/semantic.py",
                      "src/orca_agent/tools/registry.py", "src/orca_agent/tools/knowledge.py"]},
        "knowledge": {"index_version": knowledge.INDEX_VERSION, "index_sha256": knowledge.INDEX_SHA256,
            "documents": knowledge.DOCUMENTS, "metadata": knowledge.DOCUMENT_METADATA,
            "guides": knowledge.GUIDES, "manual_root": knowledge.MANUAL_ROOT,
            "manual_reference": knowledge.MANUAL_REFERENCE,
            "max_manual_reference_length": knowledge.MAX_MANUAL_REFERENCE_LENGTH,
            "retrieval_implementation": _callable_record(project, "orca_agent.tools.knowledge.retrieve"),
            "retrieved_content": "Each actual response must retain document_sha256 and raw archived content; "
                                 "this static freeze does not assert remote page bytes."},
    })


def _dependency_digest(path, counters):
    if not path.is_file() or path.is_symlink():
        raise FreezeError(f"installed dependency file missing or redirected: {path.name}")
    checked = counters.setdefault("checked_directories", set())
    for parent in path.parents:
        if parent in checked:
            continue
        if parent.is_symlink() or getattr(parent, "is_junction", lambda: False)():
            raise FreezeError(f"installed dependency file missing or redirected: {path.name}")
        checked.add(parent)
    counters["files"] += 1
    if counters["files"] > MAX_DEPENDENCY_FILES:
        raise FreezeError("dependency verification file bound exceeded")
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            counters["bytes"] += len(chunk)
            size += len(chunk)
            if counters["bytes"] > MAX_DEPENDENCY_BYTES:
                raise FreezeError("dependency verification byte bound exceeded")
            digest.update(chunk)
    return {"sha256": digest.hexdigest(), "bytes": size}


def _dependency_tree_snapshot(roots, declared, counters):
    """Bounded complete site-packages scan, including undeclared executable files."""
    bootstrap = {}
    entries = 0
    for root in sorted(roots):
        found_bootstrap = {}
        for current, directories, files in os.walk(root, followlinks=False):
            directories[:] = [name for name in directories if name != "__pycache__"]
            for name in [*directories, *files]:
                path = Path(current) / name
                if path.suffix in {".pyc", ".pyo"}:
                    continue
                entries += 1
                if entries > MAX_DEPENDENCY_FILES * 2:
                    raise FreezeError("dependency directory scan bound exceeded")
                if path.is_symlink() or getattr(path, "is_junction", lambda: False)():
                    raise FreezeError("installed dependency directory contains a redirected path")
            for name in files:
                path = Path(current) / name
                if path.suffix in {".pyc", ".pyo"} or path.resolve() in declared:
                    continue
                # uv/virtualenv adds these interpreter bootstrap files outside
                # wheel RECORDs. Bind them explicitly, without claiming wheel
                # provenance or exempting arbitrary unregistered Python files.
                if path.parent == root and name in {"_virtualenv.py", "_virtualenv.pth"}:
                    if name.endswith(".pth") and path.read_bytes().strip() != b"import _virtualenv":
                        raise FreezeError("unexpected virtualenv bootstrap import")
                    found_bootstrap[name] = _dependency_digest(path, counters)
                    continue
                raise FreezeError(f"unregistered installed dependency file: {path.relative_to(root)}")
        if found_bootstrap and set(found_bootstrap) != {"_virtualenv.py", "_virtualenv.pth"}:
            raise FreezeError("incomplete virtualenv bootstrap pair")
        if found_bootstrap:
            bootstrap[str(root)] = found_bootstrap
    return bootstrap


def dependency_snapshot():
    """Check every wheel file's bytes, including unloaded Python/native modules."""
    installed = {}
    declared = {}
    roots = set()
    counters = {"files": 0, "bytes": 0}
    for distribution in importlib.metadata.distributions():
        name = distribution.metadata["Name"].lower().replace("_", "-").replace(".", "-")
        if name in installed:
            raise FreezeError(f"multiple installed dependency identities: {name}")
        paths = {}
        for item in distribution.files or []:
            path = Path(distribution.locate_file(item)).absolute()
            if "__pycache__" in path.parts or path.suffix in {".pyc", ".pyo"}:
                continue
            if path.resolve() in paths:
                raise FreezeError(f"duplicate installed dependency file: {name}")
            paths[path.resolve()] = (path, item)
        records = [path for path in paths if path.name == "RECORD" and path.parent.name.endswith(".dist-info")]
        if len(records) != 1:
            raise FreezeError(f"installed dependency has no unique RECORD: {name}")
        root = records[0].parent.parent
        roots.add(root)
        verified = {}
        for resolved, (path, item) in paths.items():
            # Wheel scripts may live in the active environment's bin/Scripts;
            # RECORD cannot authorize reading arbitrary external paths.
            script_roots = (Path(sys.prefix).resolve() / "Scripts", Path(sys.prefix).resolve() / "bin")
            if not resolved.is_relative_to(root) and not any(
                    resolved.is_relative_to(directory) for directory in script_roots):
                raise FreezeError(f"installed dependency RECORD escapes its environment: {name}")
            actual = _dependency_digest(path, counters)
            if resolved != records[0]:
                expected = getattr(item, "hash", None)
                digest = base64.urlsafe_b64encode(bytes.fromhex(actual["sha256"])).decode().rstrip("=")
                if expected is None or expected.mode != "sha256" or expected.value != digest:
                    raise FreezeError(f"installed dependency file differs from its RECORD: {name}")
            relative = os.path.relpath(resolved, root).replace("\\", "/")
            verified[relative] = actual
            declared.setdefault(resolved, []).append((name, item))
        installed[name] = {"version": distribution.version,
            "record_path": str(records[0]), "record_sha256": verified[
                records[0].relative_to(root).as_posix()]["sha256"],
            "verified_file_count": len(verified) - 1,
            "file_count_including_record": len(verified),
            "actual_bytes": sum(row["bytes"] for row in verified.values()),
            "actual_files_sha256": _sha(_json(verified))}
    bootstrap = _dependency_tree_snapshot(roots, declared, counters)
    imports = {}
    for distribution, prefix in DEPENDENCIES.items():
        module = importlib.import_module(prefix)
        path = Path(module.__file__).resolve()
        if distribution not in {name for name, _ in declared.get(path, [])}:
            raise FreezeError(f"dependency import origin differs: {distribution}")
        imports[distribution] = {"module": prefix, "path": str(path), "sha256": _sha(path.read_bytes())}
    # Later legitimate lazy imports are checked against the same frozen RECORD;
    # their order/count is not itself a change of dependency identity.
    owners = importlib.metadata.packages_distributions()
    for name, module in tuple(sys.modules.items()):
        if name == "orca_agent" or name.startswith("orca_agent."):
            continue
        expected_owners = {owner.lower().replace("_", "-").replace(".", "-")
                           for owner in owners.get(name.split(".", 1)[0], [])}
        if not expected_owners:
            continue
        filename = getattr(module, "__file__", None)
        if not filename:
            continue
        path = Path(filename).resolve()
        candidates = declared.get(path, [])
        if not expected_owners.intersection(owner for owner, _ in candidates):
            raise FreezeError(f"loaded dependency import origin differs: {name}")
    return {"python": {"version": sys.version, "executable": str(Path(sys.executable).resolve()),
            "sha256": _sha(Path(sys.executable).read_bytes()), "platform": sys.platform},
            "installed": dict(sorted(installed.items())), "imports": imports,
            "bootstrap_files_without_wheel_provenance": bootstrap,
            "coverage": "All non-cache wheel RECORD files verified; complete bounded site-packages "
                        "scan rejects unregistered files except the explicitly frozen virtualenv bootstrap."}


def runtime_snapshot(project, config, runtime=None):
    if runtime is None:
        return {"verified": False, "issues": ["No explicit runtime inspection was supplied."],
                "coverage": "unverified; no Docker or scientific process was started by this helper"}
    value = _safe_json(runtime)
    if set(value) - {"backend", "image_id", "manifest_sha256", "verified", "issues",
                     "engine_id", "daemon_id", "manifest", "docker_endpoint"}:
        raise FreezeError("runtime must be the bounded inspect_environment result, without extra fields")
    if (value.get("backend") != config.execution.backend
            or value.get("image_id") != config.execution.image_id
            or value.get("manifest_sha256") != config.execution.manifest_sha256):
        raise FreezeError("runtime inspection differs from the frozen execution configuration")
    if value.get("verified") is not True:
        return value
    manifest = value.get("manifest")
    if (not isinstance(manifest, dict) or _sha(_json(manifest)) != value["manifest_sha256"]
            or not value.get("engine_id") or value.get("daemon_id") != value["engine_id"]
            or not value.get("docker_endpoint", "").startswith(("npipe://", "unix://"))
            or value.get("issues")):
        raise FreezeError("verified runtime inspection lacks consistent image/manifest/daemon evidence")
    expected = {
        "sdk": Path(project) / "src/orca_agent/task_sdk.py",
        "sandbox": Path(project) / "src/orca_agent/backends/container_runtime/sandbox.c",
    }
    if (manifest.get("sdk", {}).get("sha256") != _sha(expected["sdk"].read_bytes())
            or manifest.get("sandbox_source_sha256") != _sha(expected["sandbox"].read_bytes())):
        raise FreezeError("runtime SDK or sandbox differs from the candidate product")
    for name in ("helper.py", "python_task.py"):
        entry = manifest.get("files", {}).get("/opt/orca-agent-runtime/" + name, {})
        if entry.get("sha256") != _sha((expected["sandbox"].parent / name).read_bytes()):
            raise FreezeError("runtime helper differs from the candidate product")
    if any(not isinstance(manifest.get(name), dict) or not manifest[name].get("version")
           or not re.fullmatch(r"[0-9a-f]{64}", manifest[name].get("sha256", ""))
           for name in ("python", "orca", "mpi")):
        raise FreezeError("runtime manifest lacks versioned executable identities")
    value["coverage"] = "explicit read-only image/daemon inspection; not workload or isolation validation"
    return value


def build_product_manifest(project, config, *, code_commit=PRODUCT_COMMIT, runtime=None):
    """Freeze immutable product/configuration identities, excluding this manifest."""
    project = Path(project).resolve(strict=True)
    source = source_snapshot(project, code_commit)
    contract = product_contract(project)
    dependencies = dependency_snapshot()
    validate_product_imports(project, source["files"])
    return _safe_json({"schema_version": SCHEMA_VERSION, **source,
        "configuration": config.model_dump(mode="json"), "contract": contract,
        "dependencies": dependencies, "runtime": runtime_snapshot(project, config, runtime)})


def validate_product_manifest(project, manifest, config, *, runtime=None):
    """Compare the candidate's product tree, even after evidence-only commits."""
    if not isinstance(manifest, dict) or manifest.get("schema_version") != SCHEMA_VERSION:
        raise FreezeError("unsupported product freeze schema")
    expected = build_product_manifest(project, config, code_commit=manifest.get("code_commit"), runtime=runtime)
    if _json(expected) != _json(manifest):
        changed = sorted(key for key in expected if expected.get(key) != manifest.get(key))
        raise FreezeError("frozen product identity differs: " + ", ".join(changed))
    return {"status": "matched", "code_commit": expected["code_commit"],
            "manifest_sha256": _sha(_json(manifest)),
            "runtime_metadata_verified": expected["runtime"].get("verified") is True,
            "scientific_execution_performed": False}
