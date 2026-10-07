"""Read-only verification of the explicit formal acceptance freeze."""

import base64
import hashlib
import importlib
import importlib.metadata
import json
import os
import sys
from pathlib import Path

from orca_agent.config import Config, load_config
from orca_agent.store import sha256_file

PROJECT = Path(__file__).resolve().parents[2]
FREEZE = PROJECT / "docs/acceptance/phase-b/formal-freeze.json"
PACKAGES = ("openai", "httpx", "orca-pi", "pydantic", "pytest", "psutil", "filelock")
FIXED = {"pyproject.toml", "uv.lock", "config.example.toml", "AGENTS.md",
         "docs/ORCA-Agent-Project-Blueprint.md", "docs/acceptance/phase-b/coverage.json",
         "docs/acceptance/phase-b/profile.json", "docs/acceptance/phase-b/pricing-basis.json",
         "docs/acceptance/phase-b/pricing-recheck-b04.json", "docs/acceptance/phase-b/environment.json",
         "docs/acceptance/phase-b/reference-review.json", "docs/acceptance/phase-b/cases.md",
         "docs/acceptance/phase-b/runtime-profile.json"}
FIXED.add("docs/acceptance/phase-b/coverage-repair-v2.json")
FIXED.add("docs/acceptance/phase-b/budget-approval-20261007.json")
FIXED.add("docs/acceptance/phase-b/budget-approval-supplement-20261007.json")


def execution_files():
    """Enumerate relevant inputs, including files not present at freeze time."""
    names = set(FIXED)
    for directory in ("src", "tests", "docs/decisions"):
        for path in (PROJECT / directory).rglob("*"):
            if (path.is_file() and "__pycache__" not in path.parts
                    and path.suffix not in {".pyc", ".pyo"}):
                if not path.resolve().is_relative_to(PROJECT.resolve()):
                    raise ValueError("execution source resolves outside the project")
                names.add(path.relative_to(PROJECT).as_posix())
    return sorted(names)


def evaluation_config(*, science=False, config_path=None, model_profile=None):
    """Resolve the operator's actual configuration; never embed machine paths."""
    path = config_path or os.environ.get("ORCA_AGENT_CONFIG")
    loaded = load_config(Path(path) if path else None) if science else Config()
    values = loaded.model_dump()
    values["data_root"] = (PROJECT / "data/phase-b" / ("agent" if science else "reference")).resolve()
    if model_profile is not None:
        values["model_profile"] = model_profile
    return Config.model_validate(values)


def execution_budget_authority():
    """Bind approved caps and their applied receipt, never the changing spend."""
    from orca_agent.store import Store
    from tests.helpers.phase_b_budget import AcceptanceBudget

    ledger = AcceptanceBudget(Store(PROJECT / "data/phase-b/reference")).snapshot()
    authority = ledger.get("limit_authority", {})
    if authority.get("origin") != "amendment":
        raise ValueError("formal execution requires the applied cumulative budget amendment")
    return {"limits": ledger["limits"], **authority}


def runtime_environment():
    from orca_agent.context import PROMPT_VERSION
    from orca_agent.llm import BASE_URL, MODEL, TOKEN_BOUND_VERSION
    dependencies = {}
    for distribution in PACKAGES:
        prefix = "opi" if distribution == "orca-pi" else distribution
        module = importlib.import_module(prefix)
        origin = Path(module.__file__).resolve()
        installed = importlib.metadata.distribution(distribution)
        # Match the actual imported file to its distribution metadata. A local
        # module with the same version string cannot substitute for this package.
        declared = {Path(installed.locate_file(item)).resolve(): item for item in installed.files or []}
        if origin not in declared:
            raise ValueError(f"dependency import origin differs: {distribution}")
        record_files = [path for path in declared if path.name == "RECORD" and path.parent.name.endswith(".dist-info")]
        if len(record_files) != 1:
            raise ValueError(f"dependency lacks installed file identity: {distribution}")
        for name, loaded in tuple(sys.modules.items()):
            if not (name == prefix or name.startswith(prefix + ".")
                    or distribution == "pytest" and (name == "_pytest" or name.startswith("_pytest."))):
                continue
            filename = getattr(loaded, "__file__", None)
            if not filename:
                continue
            actual = Path(filename).resolve()
            item = declared.get(actual)
            digest = base64.urlsafe_b64encode(hashlib.sha256(actual.read_bytes()).digest()).decode().rstrip("=")
            if item is None or item.hash is None or item.hash.mode != "sha256" or item.hash.value != digest:
                raise ValueError(f"imported dependency file differs: {name}")
        dependencies[distribution] = {"version": installed.version,
                                      "module_sha256": sha256_file(origin),
                                      "installed_record_sha256": sha256_file(record_files[0])}
    return {"python_version": sys.version.split()[0], "packages": dependencies,
            "prompt_version": PROMPT_VERSION, "model": MODEL, "base_url": BASE_URL,
            "token_bound_version": TOKEN_BOUND_VERSION}


def validate_product_imports(record):
    root = (PROJECT / "src/orca_agent").resolve()
    for name, module in tuple(sys.modules.items()):
        if name == "orca_agent" or name.startswith("orca_agent."):
            filename = getattr(module, "__file__", None)
            if filename is None:
                namespace_paths = list(getattr(module, "__path__", []))
                if namespace_paths and all(Path(p).resolve().is_relative_to(root) for p in namespace_paths):
                    continue
                raise ValueError(f"product import origin differs: {name}")
            if not Path(filename).resolve().is_relative_to(root):
                raise ValueError(f"product import origin differs: {name}")
            relative = Path(filename).resolve().relative_to(PROJECT.resolve()).as_posix()
            if relative not in record["files"]:
                raise ValueError(f"product import is absent from freeze: {name}")


def science_environment(config):
    """Explicit scientific gate: doctor version probes, never scientific input."""
    from orca_agent.doctor import diagnose
    report = diagnose(config)
    if report["issues"] or report["orca"]["compatible"] is not True or not report["mpi"]["file_version"]:
        raise ValueError("scientific environment doctor did not verify ORCA/OPI/MS-MPI")
    return {"orca": {"path": str(config.orca_path), "version": report["orca"]["version"],
                     "sha256": sha256_file(config.orca_path)},
            "mpi": {"path": str(config.mpi_path), "version": report["mpi"]["file_version"],
                    "sha256": sha256_file(config.mpi_path)},
            "opi_version": report["packages"]["orca-pi"], "limits": report["configured_limits"]}


def freeze_hash(path, *, source_text=False):
    """Git may change source line endings; raw scientific evidence stays byte-exact."""
    data = Path(path).read_bytes()
    if source_text:
        data = data.replace(b"\r\n", b"\n")
    return hashlib.sha256(data).hexdigest()


def validate_freeze(label, *, execution=True, config=None, science=False):
    if not FREEZE.is_file():
        raise ValueError("formal evaluation requires a saved code/profile/reference freeze")
    record = json.loads(FREEZE.read_text(encoding="utf-8"))
    if record["freeze_label"] != label:
        raise ValueError("formal evaluation label differs from the active freeze")
    if not record.get("code_commit") or not record.get("files"):
        raise ValueError("formal freeze lacks its code or file manifest")
    for name, digest in record["files"].items():
        path = (PROJECT / name).resolve()
        source_text = name in record.get("source_lf_normalization", [])
        if (not path.is_relative_to(PROJECT) or not path.is_file()
                or freeze_hash(path, source_text=source_text) != digest):
            raise ValueError(f"frozen evaluation file differs: {name}")
    if execution:
        if record.get("schema_version") != 2 or not record.get("execution_environment"):
            raise ValueError("historical freeze permits static review only; a new execution freeze is required")
        if record.get("execution_files") != execution_files():
            raise ValueError("frozen execution file set differs (added or missing input)")
        if record.get("budget_authority") != execution_budget_authority():
            raise ValueError("approved cumulative budget authority differs from freeze")
        if record["execution_environment"] != runtime_environment():
            raise ValueError("actual Python, dependency, model or prompt environment differs from freeze")
        validate_product_imports(record)
        config = config or evaluation_config(science=science)
        scope = "science" if science else "model"
        if record.get("configuration", {}).get(scope) != config.model_dump(mode="json"):
            raise ValueError("effective evaluation configuration differs from freeze")
        if science:
            expected = record.get("science_environment")
            if not expected:
                raise ValueError("freeze has no verified scientific environment")
            # Reject changed binary bytes before any version-probe process.
            for key, path in (("orca", config.orca_path), ("mpi", config.mpi_path)):
                if not path or not path.is_file() or sha256_file(path) != expected[key]["sha256"]:
                    raise ValueError("scientific executable differs from freeze")
            if expected != science_environment(config):
                raise ValueError("actual ORCA/OPI/MS-MPI environment differs from freeze")
    return {"freeze_label": label, "code_commit": record["code_commit"],
            "freeze_sha256": sha256_file(FREEZE)}
