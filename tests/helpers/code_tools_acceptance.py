"""Fixed Step 6 acceptance assets, not a task solver or scientific scorer."""

from __future__ import annotations

import hashlib
import json
import os
import sys
from datetime import datetime
from pathlib import Path

from orca_agent.config import load_config
from orca_agent.store import default_environment_root
from tests.helpers import code_tools_batch as batch
from tests.helpers.code_tools_freeze import FreezeError, validate_product_manifest

DIRECTORY = "docs/acceptance/code-as-tools-step6"
ASSET_ROOTS = ("scripts", "tests/helpers", DIRECTORY)
EXCLUDED = {
    f"{DIRECTORY}/evaluation-freeze.json",
    f"{DIRECTORY}/report.md",
    f"{DIRECTORY}/validation.json",
}
REQUIRED = {
    "product-freeze.json", "product-freeze-event.json", "public-tasks.json",
    "holdout-selection.json", "scoring.md", "controls.json", "runtime.toml",
    "batch-policy.json", "knowledge-access.md", "runbook.md",
}


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def read_json(path):
    from orca_agent.llm import _strict_json
    path = Path(path)
    if path.stat().st_size > 8 * 1024 * 1024:
        raise FreezeError("acceptance JSON exceeds its byte limit")
    return _strict_json(path.read_bytes())


def write_new(path, value):
    with Path(path).open("x", encoding="utf-8", newline="\n") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2, allow_nan=False)
        stream.write("\n")


def acceptance_directory(project, directory=DIRECTORY):
    project = Path(project).resolve(strict=True)
    candidate = project / directory
    if (Path(directory).is_absolute() or ".." in Path(directory).parts
            or not candidate.resolve().is_relative_to(project / "docs/acceptance")
            or any(batch._is_link(item) for item in (candidate, *candidate.parents))):
        raise FreezeError("acceptance directory must remain inside repository docs/acceptance")
    return candidate


def candidate_batch_root(project, value):
    """Check lexical path components before resolving, including the data root."""
    project = Path(project).absolute()
    data = batch._root(project / "data")
    root = batch._root(project / value)
    if root == data or not root.is_relative_to(data):
        raise FreezeError("batch root must be a directory inside repository data")
    return root


def asset_files(project, *, directory=DIRECTORY):
    project = Path(project).resolve(strict=True)
    selected = acceptance_directory(project, directory).relative_to(project).as_posix()
    excluded = {f"{selected}/{name}" for name in ("evaluation-freeze.json", "report.md", "validation.json")}
    names = set()
    for relative in ("scripts", "tests/helpers", selected):
        directory = project / relative
        if (not directory.is_dir() or directory.is_symlink()
                or getattr(directory, "is_junction", lambda: False)()
                or not directory.resolve().is_relative_to(project)):
            raise FreezeError("missing acceptance asset directory")
        for current, dirs, files in os.walk(directory, followlinks=False):
            for name in [*dirs, *files]:
                item = Path(current) / name
                if (item.is_symlink() or getattr(item, "is_junction", lambda: False)()
                        or not item.resolve().is_relative_to(project)):
                    raise FreezeError("acceptance assets cannot redirect through links")
            dirs[:] = [name for name in dirs if name != "__pycache__"]
            for name in files:
                item = Path(current) / name
                relative_name = item.relative_to(project).as_posix()
                if item.suffix not in {".pyc", ".pyo"} and relative_name not in excluded:
                    names.add(relative_name)
    for item in (project / "tests/unit").glob("test_code_tools_*.py"):
        if item.is_symlink() or not item.resolve().is_relative_to(project):
            raise FreezeError("acceptance tests cannot redirect through links")
        names.add(item.relative_to(project).as_posix())
    # tests is currently a namespace package. Adding an initializer would execute
    # before these helpers, so its absence is part of the frozen file set too.
    initializer = project / "tests/__init__.py"
    if initializer.exists() or initializer.is_symlink():
        if initializer.is_symlink() or not initializer.is_file():
            raise FreezeError("test package initializer is not an ordinary file")
        names.add("tests/__init__.py")
    for required in REQUIRED:
        if f"{selected}/{required}" not in names:
            raise FreezeError("missing required acceptance asset: " + required)
    return {name: digest(project / name) for name in sorted(names)}


def validate_helper_imports(project):
    project = Path(project).resolve()
    for package, relative in (("tests", "tests"), ("tests.helpers", "tests/helpers")):
        module = sys.modules.get(package)
        if module is None or {Path(p).resolve() for p in module.__path__} != {project / relative}:
            raise FreezeError("acceptance package import origin changed")
    for name, module in tuple(sys.modules.items()):
        if name.startswith("tests.helpers."):
            expected = project / (name.replace(".", "/") + ".py")
            if Path(module.__file__).resolve() != expected:
                raise FreezeError("acceptance helper import origin changed")


def task_texts(project, *, directory=DIRECTORY):
    directory = acceptance_directory(project, directory)
    public = read_json(directory / "public-tasks.json")
    holdouts = read_json(directory / "holdout-selection.json")
    controls = read_json(directory / "controls.json")
    if (public["repetitions"] != 3 or holdouts["repetitions"] != 3
            or [task["id"] for task in public["tasks"]] != ["T1", "T2", "T3", "T4", "T5"]
            or [task["id"] for task in holdouts["tasks"]] != ["H1", "H2"]
            or public["seed_policy"]["seeds_by_repetition"] != [1701, 1702, 1703]):
        raise FreezeError("formal task counts or seed policy changed")
    tasks = [*public["tasks"], *holdouts["tasks"]]
    texts = {}
    for task in tasks:
        tolerances = task.get("tolerances") if task["id"].startswith("H") else public["common_tolerances"]
        if not task["original_text"].strip() or not task["reference_protocol"] or not tolerances:
            raise FreezeError("task has no fixed text, reference or tolerances")
        for repeat, seed in enumerate(public["seed_policy"]["seeds_by_repetition"], 1):
            text = (task["original_text"] + "\n\n"
                    + public["seed_policy"]["seed_suffix_template"].format(seed=seed))
            texts[batch.slot_id(task["id"], repeat, "formal")] = text
            texts[batch.slot_id(task["id"], repeat, "reference")] = (
                f"Independent reference for {task['id']} repetition {repeat}. "
                "Follow the frozen reference protocol and scoring rules; "
                "keep reference inputs and answers outside product context."
            )
    for case in controls["cases"]:
        key = batch.slot_id(case["id"], 1, "control")
        if key in texts:
            raise FreezeError("duplicate control identity")
        texts[key] = case["text"]
    if len(controls["cases"]) != 18 or len(texts) != 60:
        raise FreezeError("fixed batch requires 21 formal, 21 reference and 18 control slots")
    return texts


def check_rules(project, *, directory=DIRECTORY):
    selected = directory
    directory = acceptance_directory(project, selected)
    product = read_json(directory / "product-freeze.json")
    event = read_json(directory / "product-freeze-event.json")
    candidate_batch_root(project, event["batch_root"])
    holdouts = read_json(directory / "holdout-selection.json")
    product_sha = digest(directory / "product-freeze.json")
    if (event["product_manifest_sha256"] != product_sha
            or holdouts["product_manifest_sha256"] != product_sha
            or holdouts["product_commit"] != product["code_commit"]
            or datetime.fromisoformat(holdouts["selected_at_utc"])
            <= datetime.fromisoformat(event["frozen_at_utc"])):
        raise FreezeError("holdouts must bind the already frozen product")
    if (directory / "prior-holdout-selection.json").exists() and (
            holdouts.get("selection_kind") != "user_requested_regression"):
        raise FreezeError("regression tasks must disclose prior exposure")
    if holdouts.get("selection_kind") == "user_requested_regression":
        source = read_json(directory / "prior-holdout-selection.json")
        if (holdouts.get("novelty_status") != "previously_exposed_not_a_fresh_holdout"
                or digest(directory / "prior-holdout-selection.json") != holdouts.get("source_selection_sha256")
                or holdouts["tasks"] != source["tasks"]
                or holdouts["repetitions"] != source["repetitions"]):
            raise FreezeError("regression tasks must retain their prior selection and disclose exposure")
    elif holdouts.get("selection_kind") not in (None, "post_freeze_holdout"):
        raise FreezeError("unknown task selection kind")
    texts = task_texts(project, directory=selected)
    policy = batch._policy(read_json(directory / "batch-policy.json"))
    config = load_config(directory / "runtime.toml")
    if policy["configuration_sha256"] != batch.canonical_sha256(config.model_dump(mode="json")):
        raise FreezeError("batch configuration does not match product configuration")
    if product["configuration"] != config.model_dump(mode="json"):
        raise FreezeError("product and task configuration differ")
    slots = {batch.slot_id(s["task_id"], s["repeat"], s["category"]): s for s in policy["slots"]}
    if set(slots) != set(texts):
        raise FreezeError("batch slot list does not equal frozen tasks")
    tasks = [*read_json(directory / "public-tasks.json")["tasks"], *holdouts["tasks"]]
    reference_caps = {task["id"]: task["reference_protocol"][
        "maximum_orca_starts_including_reference_retry"] for task in tasks}
    for key, text in texts.items():
        if slots[key]["original_text_sha256"] != batch.text_sha256(text):
            raise FreezeError("batch task text mismatch: " + key)
        slot = slots[key]
        if slot["category"] == "formal" and slot["run_budget"] != config.text.budget.model_dump(mode="json"):
            raise FreezeError("formal repetitions must use the identical complete budget")
        if slot["category"] == "reference" and (
                slot["run_budget"]["model_calls"] != 0 or slot["run_budget"]["model_tokens"] != 0
                or slot["run_budget"]["orca_starts"] != reference_caps[slot["task_id"]]):
            raise FreezeError("independent reference budget differs from the fixed protocol")
    left, right = (batch.slot_id(case, 1, "control") for case in ("C06A", "C06B"))
    if texts[left] != texts[right] or slots[left]["run_budget"] != slots[right]["run_budget"]:
        raise FreezeError("paired feedback controls must differ in evidence, not instructions or budget")
    totals = {name: sum(s["reservation"][name] for s in slots.values()) for name in batch.METRICS}
    if policy["limits"] != {"rows": len(slots), **totals}:
        raise FreezeError("whole-batch limits must equal all predeclared reservations")
    if event["environment_root"] != str(default_environment_root().resolve()):
        raise FreezeError("the shared production environment root has changed")
    return {"task_slots": len(slots), "limits": policy["limits"]}


def build_evaluation_manifest(project, *, directory=DIRECTORY):
    checks = check_rules(project, directory=directory)
    return {"schema_version": 1, "product_manifest_sha256": digest(
        acceptance_directory(project, directory) / "product-freeze.json"),
        "assets": asset_files(project, directory=directory), "rules": checks,
        "scope": "integrity and bounded preparation only; no scientific scores"}


def verify(project, *, inspect_runtime=False, directory=DIRECTORY):
    """A static check never claims that cached runtime evidence was reobserved."""
    selected = directory
    directory = acceptance_directory(project, selected)
    validate_helper_imports(project)
    manifest = read_json(directory / "product-freeze.json")
    config = load_config(directory / "runtime.toml")
    if inspect_runtime:
        from orca_agent.backends.container import inspect_environment
        runtime = inspect_environment(config.execution)
    else:
        runtime = {k: v for k, v in manifest["runtime"].items() if k != "coverage"}
    product = validate_product_manifest(project, manifest, config, runtime=runtime)
    expected = build_evaluation_manifest(project, directory=selected)
    if expected != read_json(directory / "evaluation-freeze.json"):
        raise FreezeError("evaluation assets or their file set changed after freeze")
    return {**product, "runtime_reobserved": inspect_runtime,
        "runtime_metadata_verified": inspect_runtime and product["runtime_metadata_verified"],
        "evaluation_sha256": digest(directory / "evaluation-freeze.json"),
        **expected["rules"], "model_calls": 0, "orca_starts": 0}


def initialize_batch(project, *, directory=DIRECTORY):
    verified = verify(project, inspect_runtime=True, directory=directory)
    directory = acceptance_directory(project, directory)
    event = read_json(directory / "product-freeze-event.json")
    return batch.create_batch(event["batch_root"], freeze_sha256=verified["evaluation_sha256"],
        policy=read_json(directory / "batch-policy.json"),
        environment_root=event["environment_root"])


def prepare(project, *, task_id, repeat, category, directory=DIRECTORY):
    """Reserve and prepare only. It does not create or execute a product Run."""
    selected = directory
    initialize_batch(project, directory=selected)
    directory = acceptance_directory(project, selected)
    policy = read_json(directory / "batch-policy.json")
    event = read_json(directory / "product-freeze-event.json")
    key = batch.slot_id(task_id, repeat, category)
    slot = next((s for s in policy["slots"] if batch.slot_id(
        s["task_id"], s["repeat"], s["category"]) == key), None)
    if slot is None:
        raise FreezeError("task slot is not in the frozen batch")
    batch.reserve_slot(event["batch_root"], task_id=task_id, repeat=repeat, category=category)
    return batch.prepare_workspace(event["batch_root"], key, original_text=task_texts(project, directory=selected)[key],
        configuration=load_config(directory / "runtime.toml").model_dump(mode="json"),
        run_budget=slot["run_budget"])
