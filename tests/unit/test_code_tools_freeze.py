"""Offline freeze invariants; no model, scientific program or Docker starts."""

import base64
import copy
import hashlib
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from orca_agent.config import load_config
from tests.helpers import code_tools_freeze as freeze

PROJECT = Path(__file__).resolve().parents[2]


@pytest.fixture
def candidate(tmp_path, monkeypatch):
    files = {
        "pyproject.toml": b"[project]\nname='fixture'\n",
        "uv.lock": b"version=1\n",
        "src/orca_agent/agent.py": b"VALUE = 1\n",
        "src/orca_agent/static/app.js": b"const label = 'frozen';\n",
        "src/orca_agent/static/logo.bin": b"\x00\xff\r\n",
        "src/orca_agent/task_sdk.py": b"def run_orca(): pass\n",
        "src/orca_agent/backends/container_runtime/sandbox.c": b"int sandbox;\n",
        "src/orca_agent/backends/container_runtime/helper.py": b"HELPER = 1\n",
        "src/orca_agent/backends/container_runtime/python_task.py": b"TASK = 1\n",
        "scripts/container_runtime/Dockerfile": b"FROM frozen\n",
    }
    for name, raw in files.items():
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(raw)
    calls = []
    def committed(project, commit):
        assert project == tmp_path
        assert commit == freeze.PRODUCT_COMMIT
        calls.append(commit)
        return commit, copy.deepcopy(files)
    monkeypatch.setattr(freeze, "_commit_snapshot", committed)
    contract = {"tools": {"python.execute": {"definition": {"effects": ["execute_python"]}}},
        "sdk": {"public_functions": {"run_orca": {"signature": "(input_path)"}}},
        "knowledge": {"guides": {"guide_input": "Declare source files."}, "manual_root": "https://example.invalid/v1/"},
        "prompt": {"version": "fixture-1"}}
    dependencies = {"python": {"version": "fixture"}, "imports": {"example": {"path": "fixed"}}}
    monkeypatch.setattr(freeze, "product_contract", lambda _: copy.deepcopy(contract))
    monkeypatch.setattr(freeze, "dependency_snapshot", lambda: copy.deepcopy(dependencies))
    monkeypatch.setattr(freeze, "validate_product_imports", lambda *_: None)
    config = load_config(PROJECT / "config.code.example.toml")
    manifest = freeze.build_product_manifest(tmp_path, config)
    return SimpleNamespace(root=tmp_path, files=files, config=config, manifest=manifest,
        contract=contract, dependencies=dependencies, commits=calls)


def test_manifest_covers_non_python_resources_and_all_product_files(candidate):
    assert set(candidate.manifest["files"]) == set(candidate.files)
    for name, raw in candidate.files.items():
        row = candidate.manifest["files"][name]
        assert row["sha256"] == hashlib.sha256(raw).hexdigest()
        assert row["bytes"] == len(raw)
    result = freeze.validate_product_manifest(candidate.root, candidate.manifest, candidate.config)
    assert result["status"] == "matched"
    assert result["runtime_metadata_verified"] is False
    assert result["scientific_execution_performed"] is False


@pytest.mark.parametrize("name,change", [
    ("src/orca_agent/new_solver.py", "add"),
    ("src/orca_agent/static/new_example.txt", "add"),
    ("src/orca_agent/static/logo.bin", "modify"),
    ("src/orca_agent/static/app.js", "delete"),
    ("scripts/container_runtime/Dockerfile", "modify"),
])
def test_added_removed_and_changed_product_bytes_are_rejected(candidate, name, change):
    path = candidate.root / name
    if change == "delete":
        path.unlink()
    else:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"changed")
    with pytest.raises(freeze.FreezeError, match="file set|working product"):
        freeze.validate_product_manifest(candidate.root, candidate.manifest, candidate.config)


def test_evidence_only_additions_do_not_require_head_to_equal_product_commit(candidate):
    path = candidate.root / "docs/acceptance/current/product-manifest.json"
    path.parent.mkdir(parents=True)
    path.write_bytes(freeze._json(candidate.manifest))
    (candidate.root / "tests").mkdir()
    (candidate.root / "tests/acceptance_review.py").write_text("# evidence driver only\n")
    freeze.validate_product_manifest(candidate.root, candidate.manifest, candidate.config)
    assert candidate.commits == [freeze.PRODUCT_COMMIT, freeze.PRODUCT_COMMIT]
    assert str(path) not in str(candidate.manifest["files"])


def test_cache_files_are_excluded_but_other_new_resources_are_not(candidate):
    cache = candidate.root / "src/orca_agent/__pycache__"
    cache.mkdir()
    (cache / "agent.cpython-311.pyc").write_bytes(b"cache")
    (candidate.root / "src/orca_agent/agent.pyc").write_bytes(b"legacy cache")
    freeze.validate_product_manifest(candidate.root, candidate.manifest, candidate.config)
    (candidate.root / "src/orca_agent/recipe.json").write_text('{"recipe":"new"}')
    with pytest.raises(freeze.FreezeError, match="file set"):
        freeze.validate_product_manifest(candidate.root, candidate.manifest, candidate.config)


def test_crlf_git_checkout_match_keeps_actual_bytes_frozen(candidate):
    path = candidate.root / "src/orca_agent/agent.py"
    path.write_bytes(candidate.files["src/orca_agent/agent.py"].replace(b"\n", b"\r\n"))
    manifest = freeze.build_product_manifest(candidate.root, candidate.config)
    assert manifest["files"]["src/orca_agent/agent.py"]["commit_comparison"] == "utf8-crlf-to-lf"
    freeze.validate_product_manifest(candidate.root, manifest, candidate.config)
    path.write_bytes(candidate.files["src/orca_agent/agent.py"])
    with pytest.raises(freeze.FreezeError, match="identity differs: files"):
        freeze.validate_product_manifest(candidate.root, manifest, candidate.config)


def test_binary_newlines_never_receive_source_normalization(candidate):
    path = candidate.root / "src/orca_agent/static/logo.bin"
    path.write_bytes(candidate.files["src/orca_agent/static/logo.bin"].replace(b"\r\n", b"\n"))
    with pytest.raises(freeze.FreezeError, match="working product"):
        freeze.build_product_manifest(candidate.root, candidate.config)


def test_source_symlink_is_rejected_even_when_target_has_identical_bytes(candidate, monkeypatch):
    original = Path.is_symlink
    target = candidate.root / "src/orca_agent/agent.py"
    monkeypatch.setattr(Path, "is_symlink", lambda path: path == target or original(path))
    with pytest.raises(freeze.FreezeError, match="redirects"):
        freeze.product_files(candidate.root)


@pytest.mark.parametrize("section", ["tools", "sdk", "knowledge", "prompt"])
def test_tool_sdk_knowledge_and_prompt_changes_invalidate_freeze(candidate, section):
    candidate.contract[section]["new"] = "changed runtime contract"
    with pytest.raises(freeze.FreezeError, match="identity differs: contract"):
        freeze.validate_product_manifest(candidate.root, candidate.manifest, candidate.config)


def test_frozen_budget_and_actual_dependency_import_origin_cannot_change(candidate):
    changed = candidate.config.model_copy(deep=True)
    changed.text.budget.model_calls += 1
    with pytest.raises(freeze.FreezeError, match="configuration"):
        freeze.validate_product_manifest(candidate.root, candidate.manifest, changed)
    candidate.dependencies["imports"]["example"]["path"] = "shadowed"
    with pytest.raises(freeze.FreezeError, match="dependencies"):
        freeze.validate_product_manifest(candidate.root, candidate.manifest, candidate.config)


def inspected_runtime(candidate):
    def digest(name):
        return hashlib.sha256((candidate.root / name).read_bytes()).hexdigest()
    manifest = {"schema_version": 1, "isolation_policy": "linux-container-v1",
        "sdk": {"sha256": digest("src/orca_agent/task_sdk.py")},
        "sandbox_source_sha256": digest("src/orca_agent/backends/container_runtime/sandbox.c"),
        "files": {"/opt/orca-agent-runtime/" + name: {
            "sha256": digest("src/orca_agent/backends/container_runtime/" + name)}
            for name in ("helper.py", "python_task.py")},
        **{name: {"version": "fixture", "sha256": "a" * 64} for name in ("python", "orca", "mpi")}}
    sha = hashlib.sha256(freeze._json(manifest)).hexdigest()
    config = candidate.config.model_copy(update={"execution":
        candidate.config.execution.model_copy(update={"manifest_sha256": sha})})
    runtime = {"backend": "linux-container", "verified": True, "issues": [],
        "image_id": config.execution.image_id, "manifest_sha256": sha, "manifest": manifest,
        "engine_id": "engine-fixture", "daemon_id": "engine-fixture", "docker_endpoint": "unix:///fixture.sock"}
    return config, runtime


def test_explicit_runtime_metadata_is_bound_without_claiming_workload_validation(candidate):
    config, runtime = inspected_runtime(candidate)
    manifest = freeze.build_product_manifest(candidate.root, config, runtime=runtime)
    result = freeze.validate_product_manifest(candidate.root, manifest, config, runtime=runtime)
    assert result["runtime_metadata_verified"] is True
    assert "not workload or isolation validation" in manifest["runtime"]["coverage"]
    assert result["scientific_execution_performed"] is False
    with pytest.raises(freeze.FreezeError, match="runtime"):
        freeze.validate_product_manifest(candidate.root, manifest, config)


@pytest.mark.parametrize("change", ["image", "manifest", "sdk", "helper", "daemon", "credentials"])
def test_changed_or_incomplete_runtime_evidence_is_rejected(candidate, change):
    config, runtime = inspected_runtime(candidate)
    if change == "image":
        runtime["image_id"] = "sha256:" + "c" * 64
    elif change == "manifest":
        runtime["manifest"]["python"]["version"] = "changed"
    elif change in {"sdk", "helper"}:
        if change == "sdk":
            runtime["manifest"]["sdk"]["sha256"] = "d" * 64
        else:
            runtime["manifest"]["files"]["/opt/orca-agent-runtime/helper.py"]["sha256"] = "d" * 64
        sha = hashlib.sha256(freeze._json(runtime["manifest"])).hexdigest()
        runtime["manifest_sha256"] = sha
        config.execution = config.execution.model_copy(update={"manifest_sha256": sha})
    elif change == "daemon":
        runtime.pop("engine_id")
    else:
        runtime["credentials"] = "must never persist"
    with pytest.raises(freeze.FreezeError):
        freeze.build_product_manifest(candidate.root, config, runtime=runtime)


def test_runtime_unavailable_preserves_failure_instead_of_marking_verified(candidate):
    runtime = {"backend": "linux-container", "image_id": candidate.config.execution.image_id,
        "manifest_sha256": candidate.config.execution.manifest_sha256, "verified": False,
        "issues": ["Docker unavailable"]}
    manifest = freeze.build_product_manifest(candidate.root, candidate.config, runtime=runtime)
    assert manifest["runtime"]["verified"] is False
    assert manifest["runtime"]["issues"] == ["Docker unavailable"]


def test_actual_product_contract_has_single_registry_and_explicit_sdk_functions():
    from orca_agent.tools.registry import TOOLS
    contract = freeze.product_contract(PROJECT)
    assert set(contract["tools"]) == set(TOOLS)
    for name, row in contract["tools"].items():
        assert row["definition"] == TOOLS[name].tool.model_dump(mode="json")
        assert row["functions"]["implementation"]["file"].startswith("src/orca_agent/")
    assert set(contract["sdk"]["public_functions"]) == {"run_orca", "receipt_source", "checkpoint"}
    assert contract["knowledge"]["manual_root"].endswith("/orca/6.1/manual/")
    assert set(contract["knowledge"]["guides"]) == {"guide_input", "guide_method", "guide_diagnostics"}
    freeze.validate_product_imports(PROJECT, freeze.product_files(PROJECT))


def test_registered_function_code_replacement_cannot_hide_behind_original_filename(monkeypatch):
    from orca_agent.tools import workspace
    original = workspace.write
    replacement = compile("def write(*args, **kwargs):\n    return 'injected'\n",
                          original.__code__.co_filename, "exec")
    code = next(code for code in freeze._compiled_codes(replacement) if code.co_name == "write")
    monkeypatch.setattr(original, "__code__", code)
    with pytest.raises(freeze.FreezeError, match="loaded implementation bytecode differs"):
        freeze._callable_record(PROJECT, "orca_agent.tools.workspace.write")


def test_registered_nested_code_constant_change_is_rejected(monkeypatch):
    from orca_agent import task_sdk
    original = task_sdk.receipt_source
    # Replacing a constant preserves the original source path, function name,
    # signature, and the bytecode instruction stream.
    constants = list(original.__code__.co_consts)
    index = next(index for index, item in enumerate(constants) if isinstance(item, str))
    constants[index] += " mutated"
    monkeypatch.setattr(original, "__code__", original.__code__.replace(co_consts=tuple(constants)))
    with pytest.raises(freeze.FreezeError, match="loaded implementation bytecode differs"):
        freeze._callable_record(PROJECT, "orca_agent.task_sdk.receipt_source")


def test_product_module_loaded_from_outside_candidate_is_rejected(tmp_path, monkeypatch):
    path = tmp_path / "injected.py"
    path.write_text("VALUE = 1\n")
    module = SimpleNamespace(__name__="orca_agent.injected", __file__=str(path))
    monkeypatch.setitem(sys.modules, "orca_agent.injected", module)
    with pytest.raises(freeze.FreezeError, match="import origin differs"):
        freeze.validate_product_imports(PROJECT, freeze.product_files(PROJECT))


@pytest.fixture
def fake_dependency(tmp_path, monkeypatch):
    root = tmp_path / "site-packages"
    module = root / "fake/__init__.py"
    module.parent.mkdir(parents=True)
    module.write_bytes(b"VERSION = 1\n")
    record = root / "fake-1.dist-info/RECORD"
    record.parent.mkdir()
    record.write_text("fixture record")
    digest = base64.urlsafe_b64encode(hashlib.sha256(module.read_bytes()).digest()).decode().rstrip("=")
    entries = [SimpleNamespace(path=module, hash=SimpleNamespace(mode="sha256", value=digest)),
               SimpleNamespace(path=record, hash=None)]
    distribution = SimpleNamespace(metadata={"Name": "fake"}, version="1", files=entries,
        locate_file=lambda item: item.path)
    actual = SimpleNamespace(__file__=str(module))
    monkeypatch.setattr(freeze.importlib.metadata, "distributions", lambda: [distribution])
    monkeypatch.setattr(freeze.importlib.metadata, "packages_distributions", lambda: {"fake": ["fake"]})
    monkeypatch.setattr(freeze.importlib, "import_module", lambda _: actual)
    monkeypatch.setattr(freeze, "DEPENDENCIES", {"fake": "fake"})
    monkeypatch.setitem(sys.modules, "fake", actual)
    def add(name, raw):
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(raw)
        digest = base64.urlsafe_b64encode(hashlib.sha256(raw).digest()).decode().rstrip("=")
        entries.append(SimpleNamespace(path=path, hash=SimpleNamespace(mode="sha256", value=digest)))
        return path
    return SimpleNamespace(module=module, actual=actual, distribution=distribution, root=root, add=add)


def test_dependency_snapshot_binds_installed_record_and_actual_import(fake_dependency):
    snapshot = freeze.dependency_snapshot()
    assert snapshot["installed"]["fake"]["version"] == "1"
    assert snapshot["installed"]["fake"]["verified_file_count"] == 1
    assert snapshot["imports"]["fake"]["path"] == str(fake_dependency.module.resolve())
    fake_dependency.module.write_bytes(b"VERSION = 2\n")
    with pytest.raises(freeze.FreezeError, match="installed dependency file differs"):
        freeze.dependency_snapshot()


@pytest.mark.parametrize("name", ["fake/lazy.py", "fake/native.pyd", "fake.libs/native.dll",
                                 "fake/libnative.so", "fake/data/table.json"])
@pytest.mark.parametrize("change", ["tamper", "missing"])
def test_every_unimported_record_file_is_verified(fake_dependency, name, change):
    path = fake_dependency.add(name, b"registered bytes")
    snapshot = freeze.dependency_snapshot()
    assert snapshot["installed"]["fake"]["verified_file_count"] == 2
    if change == "tamper":
        path.write_bytes(b"changed bytes")
    else:
        path.unlink()
    with pytest.raises(freeze.FreezeError, match="file differs|file missing"):
        freeze.dependency_snapshot()


@pytest.mark.parametrize("name", ["fake/injected.py", "injected.pth", "fake/new.dll",
                                 "fake.libs/libnew.so", "fake/data/new.json"])
def test_unregistered_package_files_are_rejected(fake_dependency, name):
    path = fake_dependency.root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"new unregistered bytes")
    with pytest.raises(freeze.FreezeError, match="unregistered installed dependency file"):
        freeze.dependency_snapshot()


def test_dependency_record_without_content_hash_is_rejected(fake_dependency):
    fake_dependency.add("fake/native.dll", b"native library")
    fake_dependency.distribution.files[-1].hash = None
    with pytest.raises(freeze.FreezeError, match="file differs from its RECORD"):
        freeze.dependency_snapshot()


def test_dependency_cache_exclusion_and_explicit_bootstrap_identity(fake_dependency):
    root = fake_dependency.root
    (root / "fake/__pycache__").mkdir()
    (root / "fake/__pycache__/lazy.pyc").write_bytes(b"cache")
    (root / "fake/legacy.pyc").write_bytes(b"cache")
    (root / "_virtualenv.pth").write_bytes(b"import _virtualenv\n")
    (root / "_virtualenv.py").write_bytes(b"# interpreter bootstrap\n")
    snapshot = freeze.dependency_snapshot()
    bootstrap = snapshot["bootstrap_files_without_wheel_provenance"][str(root)]
    assert set(bootstrap) == {"_virtualenv.pth", "_virtualenv.py"}
    assert bootstrap["_virtualenv.py"]["sha256"] == hashlib.sha256(b"# interpreter bootstrap\n").hexdigest()
    (root / "_virtualenv.pth").write_bytes(b"import injected")
    with pytest.raises(freeze.FreezeError, match="bootstrap import"):
        freeze.dependency_snapshot()


def test_dependency_scan_is_bounded(fake_dependency, monkeypatch):
    monkeypatch.setattr(freeze, "MAX_DEPENDENCY_BYTES", 1)
    with pytest.raises(freeze.FreezeError, match="byte bound exceeded"):
        freeze.dependency_snapshot()


def test_dependency_shadow_outside_installed_distribution_is_rejected(fake_dependency, tmp_path):
    shadow = tmp_path / "shadow.py"
    shadow.write_bytes(b"VERSION = 1\n")
    fake_dependency.actual.__file__ = str(shadow)
    with pytest.raises(freeze.FreezeError, match="dependency import origin differs"):
        freeze.dependency_snapshot()


def test_lazy_dependency_submodule_shadow_is_rejected(fake_dependency, tmp_path, monkeypatch):
    shadow = tmp_path / "injected.py"
    shadow.write_text("VALUE = 1\n")
    monkeypatch.setitem(sys.modules, "fake.lazy", SimpleNamespace(__file__=str(shadow)))
    with pytest.raises(freeze.FreezeError, match="loaded dependency import origin differs"):
        freeze.dependency_snapshot()


def test_git_reader_uses_concrete_candidate_and_rejects_symbolic_refs(monkeypatch, tmp_path):
    calls = []
    def git(_, *args):
        calls.append(args)
        if args[0] == "rev-parse":
            return (freeze.PRODUCT_COMMIT + "\n").encode()
        if args[0] == "ls-tree":
            return b"100644 blob " + b"a" * 40 + b"\tsrc/orca_agent/asset.bin\0"
        return b"\xff\x00"
    monkeypatch.setattr(freeze, "_git", git)
    commit, files = freeze._commit_snapshot(tmp_path, freeze.PRODUCT_COMMIT)
    assert commit == freeze.PRODUCT_COMMIT
    assert files == {"src/orca_agent/asset.bin": b"\xff\x00"}
    assert calls[0] == ("rev-parse", "--verify", freeze.PRODUCT_COMMIT + "^{commit}")
    with pytest.raises(freeze.FreezeError, match="concrete hexadecimal"):
        freeze._commit_snapshot(tmp_path, "HEAD")
