"""Offline Step 6 preparation/accounting; no model, subprocess or ORCA launches."""

import json
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from pathlib import Path

import pytest

from orca_agent.config import Config
from orca_agent.models import BudgetLimits, Goal, Request
from orca_agent.store import Store
from tests.helpers import code_tools_batch as batch


def policy(*, count=3, configuration=None):
    config = configuration or Config()
    envelope = dict(model_calls=2, model_tokens=100, orca_starts=3,
                    internal_work_units=20, wall_seconds=60)
    texts = {f"T{index + 1}": f"Independent user request {index + 1}." for index in range(count)}
    value = {"version": 1, "batch_id": "offline-acceptance",
             "configuration_sha256": batch.canonical_sha256(config.model_dump(mode="json")),
             "limits": {"rows": count, **{name: amount * count for name, amount in envelope.items()}},
             "slots": [{"task_id": task, "repeat": 1, "category": category,
                        "original_text_sha256": batch.text_sha256(text), "reservation": dict(envelope),
                        "run_budget": budget()}
                       for (task, text), category in zip(texts.items(),
                           ["formal", "reference", "control"] * count)]}
    return value, config, texts


@pytest.fixture
def prepared(tmp_path):
    value, config, texts = policy()
    root, environment = tmp_path / "batch", tmp_path / "environment"
    batch.create_batch(root, freeze_sha256="a" * 64, policy=value, environment_root=environment)
    return root, environment, value, config, texts


def reserve(root, index=0, value=None):
    row = (value or policy()[0])["slots"][index]
    return batch.reserve_slot(root, row["task_id"], row["repeat"], row["category"])["slot_id"]


def budget(**changes):
    values = dict(model_calls=2, model_tokens=100, orca_starts=3,
                  extra_orca_starts=2, internal_work_units=20, run_seconds=60, file_writes=200)
    values.update(changes)
    return BudgetLimits(**values).model_dump(mode="json")


def workspace(prepared, index=0):
    root, _, value, config, texts = prepared
    key = reserve(root, index, value)
    return key, batch.prepare_workspace(root, key, original_text=texts[value["slots"][index]["task_id"]],
        configuration=config.model_dump(mode="json"), run_budget=budget())


def create_shell(manifest):
    directory = Path(manifest["data_root"]).parent
    config = Config.model_validate_json((directory / "config.json").read_bytes())
    text = (directory / "original.txt").read_bytes().decode("utf-8")
    store = Store(manifest["data_root"], environment_root=manifest["environment_root"],
                  configuration=config.run_configuration())
    request = Request(original_text=text, charge=None, multiplicity=None, method=None, basis=None,
        normalization_status="pending", conditions_source={},
        goals=[Goal(id="raw_request", port="unresolved", minimum_check_version="unresolved-1",
                    original_text=text, unresolved=["missing:goal_definition"])])
    run = store.create_run(request, None, config.text.permission, config.text.budget,
                           science_baseline_policy="first_science_plan")
    return store, run


def test_exact_batch_reopen_does_not_reset_fixed_reservations(prepared):
    root, environment, value, _, _ = prepared
    key = reserve(root)
    first = batch.snapshot(root)
    assert reserve(root) == key
    again = batch.create_batch(root, freeze_sha256="a" * 64, policy=value, environment_root=environment)
    assert again == first
    assert len(again["rows"]) == 1
    assert again["totals"]["occupied"] == value["slots"][0]["reservation"]
    assert again["totals"]["unknown_slots"] == dict.fromkeys(batch.METRICS, 1)


@pytest.mark.parametrize("change", ["root", "freeze", "limits", "text", "config", "missing_ledger"])
def test_fixed_environment_anchor_rejects_identity_policy_and_path_reset(prepared, change):
    root, environment, value, _, _ = prepared
    freeze = "a" * 64
    if change == "root":
        root = root.parent / "new-batch"
    elif change == "freeze":
        freeze = "b" * 64
    elif change == "limits":
        value["limits"]["model_calls"] += 1
    elif change == "text":
        value["slots"][0]["original_text_sha256"] = "c" * 64
    elif change == "config":
        value["configuration_sha256"] = "d" * 64
    else:
        (root / "ledger.json").unlink()
    with pytest.raises(ValueError, match="anchor differs|missing"):
        batch.create_batch(root, freeze_sha256=freeze, policy=value, environment_root=environment)


@pytest.mark.parametrize("task,repeat,category", [
    ("T1", 2, "formal"), ("T1", 1, "reference"), ("T9", 1, "formal"),
    ("../T1", 1, "formal"), ("T1", True, "formal"), ("T1", 1, "development"),
])
def test_unfrozen_repetitions_categories_and_alias_ids_cannot_consume_new_slots(prepared, task, repeat, category):
    with pytest.raises(ValueError, match="frozen|identity|category"):
        batch.reserve_slot(prepared[0], task, repeat, category)
    assert not batch.snapshot(prepared[0])["rows"]


def test_model_reference_and_control_costs_share_one_nonrefundable_limit(prepared):
    root, _, value, _, _ = prepared
    keys = [reserve(root, index, value) for index in range(3)]
    batch.settle_slot(root, keys[0], known_usage=dict.fromkeys(batch.METRICS, 0), unknown_fields={})
    result = batch.snapshot(root)
    assert result["totals"]["occupied"] == {name: value["limits"][name] for name in batch.METRICS}
    assert {row["slot"]["category"] for row in result["rows"].values()} == batch.CATEGORIES
    assert result["totals"]["unknown_slots"] == dict.fromkeys(batch.METRICS, 2)
    assert result["rows"][keys[0]]["settlement"]["usage"] == dict.fromkeys(batch.METRICS, 0)


def test_unknown_usage_is_null_with_reason_and_keeps_entire_reservation(prepared):
    root = prepared[0]
    key = reserve(root)
    unknown = {"model_tokens": "provider supplied no usage", "wall_seconds": "worker receipt unavailable"}
    known = {"model_calls": 1, "orca_starts": 2, "internal_work_units": 8}
    batch.settle_slot(root, key, known_usage=known, unknown_fields=unknown)
    result = batch.snapshot(root)
    settlement = result["rows"][key]["settlement"]
    assert settlement["usage"] == {**known, "model_tokens": None, "wall_seconds": None}
    assert settlement["unknown_reasons"] == unknown
    assert result["totals"]["occupied"] == prepared[2]["slots"][0]["reservation"]
    assert result["totals"]["unknown_slots"]["model_tokens"] == 1
    assert batch.settle_slot(root, key, known_usage=known, unknown_fields=unknown)["settlement"] == settlement
    with pytest.raises(ValueError, match="cannot be replaced"):
        batch.settle_slot(root, key, known_usage=dict.fromkeys(batch.METRICS, 0), unknown_fields={})


@pytest.mark.parametrize("bad", [True, -1, float("nan"), float("inf"), "3"])
def test_invalid_usage_cannot_be_reported_as_known(prepared, bad):
    key = reserve(prepared[0])
    usage = dict.fromkeys(batch.METRICS, 0)
    usage["orca_starts"] = bad
    with pytest.raises(ValueError, match="invalid usage"):
        batch.settle_slot(prepared[0], key, known_usage=usage, unknown_fields={})


def test_usage_overrun_is_recorded_and_blocks_further_reservations(prepared):
    root = prepared[0]
    key = reserve(root)
    usage = dict.fromkeys(batch.METRICS, 0)
    usage["orca_starts"] = 100
    batch.settle_slot(root, key, known_usage=usage, unknown_fields={})
    result = batch.snapshot(root)
    assert result["rows"][key]["settlement"]["usage"]["orca_starts"] == 100
    assert result["overrun_metrics"] == ["orca_starts"]
    with pytest.raises(ValueError, match="whole-batch reservation exhausted"):
        reserve(root, 1)


def test_run_id_cannot_replace_a_failed_run_or_be_shared_across_repetitions(prepared):
    root = prepared[0]
    first, manifest = workspace(prepared)
    second, _ = workspace(prepared, 1)
    _, run = create_shell(manifest)
    assert batch.bind_run(root, first, run.id)["run_id"] == run.id
    assert batch.bind_run(root, first, run.id)["run_id"] == run.id
    with pytest.raises(ValueError, match="cannot be replaced"):
        batch.bind_run(root, first, "run_retry")
    with pytest.raises(ValueError, match="cannot share"):
        batch.bind_run(root, second, run.id.upper())


@pytest.mark.parametrize("name", ["solver.py", "plan.json"])
def test_run_binding_rejects_injected_data_before_first_execution(prepared, name):
    key, manifest = workspace(prepared)
    injected = Path(manifest["data_root"]) / name
    injected.write_text("previous task content", encoding="utf-8")
    with pytest.raises(ValueError, match="injected or previous"):
        batch.bind_run(prepared[0], key, "run_new")
    assert batch.snapshot(prepared[0])["rows"][key]["run_id"] is None


def test_resume_reuses_binding_without_resetting_product_data(prepared):
    key, manifest = workspace(prepared)
    _, run = create_shell(manifest)
    batch.bind_run(prepared[0], key, run.id)
    output = Path(manifest["data_root"]) / "retained-output.txt"
    output.write_text("original failure and evidence", encoding="utf-8")
    assert batch.bind_run(prepared[0], key, run.id)["run_id"] == run.id
    assert output.read_text(encoding="utf-8") == "original failure and evidence"


def test_real_store_create_run_shell_binds_before_any_execution(prepared):
    key, manifest = workspace(prepared)
    store, run = create_shell(manifest)
    assert not run.agent_enabled
    assert run.plan_id is None and not run.attempts and not run.model_records
    assert batch.bind_run(prepared[0], key, run.id)["run_id"] == run.id
    assert store.load_run(run.id) == run
    assert batch.snapshot(prepared[0])["totals"]["known_usage"] == dict.fromkeys(batch.METRICS, 0)


@pytest.mark.parametrize("text", ["Independent user request.", "Optimize the water molecule."])
@pytest.mark.parametrize("workflow_policy", [None, "simplified-1"])
def test_pure_text_initialization_is_bindable_without_model_or_environment_launch(
        tmp_path, monkeypatch, text, workflow_policy):
    from orca_agent import doctor
    from orca_agent.natural import initialize_text

    def forbidden(*args, **kwargs):
        raise AssertionError("initialization must not launch or probe a scientific environment")

    monkeypatch.setattr(doctor, "capture_environment", forbidden)
    config = Config(workflow_policy=workflow_policy)
    config.text.enabled = True
    config.text.permission.model_execution = True
    config.text.permission.scientific_execution = True
    value, config, texts = policy(configuration=config)
    texts["T1"] = text
    value["slots"][0]["original_text_sha256"] = batch.text_sha256(text)
    root, environment = tmp_path / "batch", tmp_path / "environment"
    batch.create_batch(root, freeze_sha256="a" * 64, policy=value, environment_root=environment)
    key, manifest = workspace((root, environment, value, config, texts))
    prepared_config = Config.model_validate_json(
        (Path(manifest["data_root"]).parent / "config.json").read_bytes())
    store = Store(manifest["data_root"], environment_root=manifest["environment_root"],
                  configuration=prepared_config.run_configuration())
    run = initialize_text(store, prepared_config, texts["T1"])
    assert run.agent_enabled and not run.model_records and not run.attempts
    assert store.read_control(run.id)["messages"][0]["text"] == texts["T1"]
    assert batch.bind_run(root, key, run.id)["run_id"] == run.id
    assert store.load_run(run.id) == run


@pytest.mark.parametrize("change", ["missing", "version", "source", "extra", "other", "legacy"])
def test_initial_program_marker_is_exact_and_requires_its_frozen_workflow(tmp_path, change):
    from orca_agent.natural import initialize_text

    config = Config(workflow_policy=None if change == "legacy" else "simplified-1")
    config.text.enabled = True
    config.text.permission.model_execution = True
    value, config, texts = policy(configuration=config)
    root, environment = tmp_path / "batch", tmp_path / "environment"
    batch.create_batch(root, freeze_sha256="a" * 64, policy=value, environment_root=environment)
    key, manifest = workspace((root, environment, value, config, texts))
    prepared_config = Config.model_validate_json(
        (Path(manifest["data_root"]).parent / "config.json").read_bytes())
    store = Store(manifest["data_root"], environment_root=environment,
                  configuration=prepared_config.run_configuration())
    run = initialize_text(store, prepared_config, texts["T1"])
    path = store.path(f"runs/{run.id}/request-revisions/1.json")
    request = json.loads(path.read_bytes())
    marker = {"version": "simplified-1", "source": "run_configuration"}
    if change == "version":
        marker["version"] = "other-policy"
    elif change == "source":
        marker["source"] = "model_proposal"
    elif change == "extra":
        marker["injected"] = "prior scientific solution"
    evidence = {} if change == "missing" else {"workflow_policy": marker}
    if change == "other":
        evidence["request.method"] = {"value": "HF", "source": "explicit"}
    request["condition_evidence"] = evidence
    path.write_text(json.dumps(request), encoding="utf-8")
    original = path.read_bytes()
    with pytest.raises(ValueError, match="fresh Run Request"):
        batch.bind_run(root, key, run.id)
    assert path.read_bytes() == original
    assert batch.snapshot(root)["rows"][key]["run_id"] is None
    assert not run.model_records and not run.attempts


@pytest.mark.parametrize("case", ["missing", "different_id", "second_run"])
def test_binding_requires_the_unique_actual_run_created_in_its_workspace(prepared, case):
    key, manifest = workspace(prepared)
    run_id = "run_not_created"
    if case != "missing":
        _, run = create_shell(manifest)
        if case == "second_run":
            create_shell(manifest)
            run_id = run.id
    with pytest.raises(ValueError, match="no unique Run"):
        batch.bind_run(prepared[0], key, run_id)
    assert batch.snapshot(prepared[0])["rows"][key]["run_id"] is None


@pytest.mark.parametrize("field,value", [
    ("model_calls", 1), ("model_tokens_unknown", 1), ("orca_starts_reserved", 1),
    ("orca_starts_actual", 1), ("internal_work_units", 1), ("elapsed_seconds", 1),
    ("resource_usage_complete", False), ("plan_revisions", 1),
])
def test_binding_rejects_all_consumed_or_unknown_usage_before_execution(prepared, field, value):
    key, manifest = workspace(prepared)
    store, run = create_shell(manifest)
    setattr(run.usage, field, value)
    store.save_run(run)
    with pytest.raises(ValueError, match="before binding"):
        batch.bind_run(prepared[0], key, run.id)


@pytest.mark.parametrize("change", [
    "original", "configuration", "permission", "budget", "budget_file",
    "plan", "attempt", "decision", "request_revision", "state", "deadline",
    "structured_goal", "request_solution", "method", "system", "future_creation",
])
def test_binding_rejects_changed_snapshots_and_prior_actions(prepared, change):
    key, manifest = workspace(prepared)
    store, run = create_shell(manifest)
    directory = store.path(f"runs/{run.id}")
    path = directory / "run.json"
    value = json.loads(path.read_text(encoding="utf-8"))
    if change in {"original", "structured_goal", "request_solution", "method", "system"}:
        path = directory / "request-revisions" / "1.json"
        value = json.loads(path.read_text(encoding="utf-8"))
        if change == "original":
            value["original_text"] = "Different task with a prewritten answer."
        elif change == "structured_goal":
            value["goals"][0]["conditions"] = {"prewritten_input": "! HF STO-3G"}
        elif change == "request_solution":
            value["conditions"] = {"answer": "Run the solver from a prior task."}
        elif change == "method":
            value["method"] = "HF"
        else:
            value["systems"] = [{"id": "precomputed", "identity": {"answer": "old solution"}}]
    elif change == "configuration":
        value["configuration"]["mechanical_progression"] = False
    elif change == "permission":
        value["permission"]["code_execution"] = True
    elif change == "budget":
        value["budget"]["attempts_per_step"] += 1
    elif change == "budget_file":
        path = directory / "budget.json"
        value = json.loads(path.read_text(encoding="utf-8"))
        value["code_retries"] += 1
    elif change == "plan":
        value["plan_id"], value["plan_version"] = "plan_precomputed", 1
    elif change == "attempt":
        value["attempts"] = [{"step_id": "step1", "logical_id": "logical1", "number": 1,
            "tool": "python.execute", "input_fingerprint": "a" * 64, "directory": "attempts/test"}]
    elif change == "decision":
        value["decisions"] = [{"action": "replace_plan", "answer": "prior task solution"}]
    elif change == "request_revision":
        value["request_version"] = 2
    elif change == "state":
        value["state"] = "running"
    elif change == "deadline":
        from datetime import timedelta
        value["deadline"] = (run.deadline + timedelta(seconds=1)).isoformat()
    else:
        from datetime import timedelta
        value["created_at"] = (run.created_at + timedelta(days=1)).isoformat()
        value["deadline"] = (run.deadline + timedelta(days=1)).isoformat()
    path.write_text(json.dumps(value), encoding="utf-8")
    with pytest.raises(ValueError, match="fresh Run|before binding|Run configuration"):
        batch.bind_run(prepared[0], key, run.id)
    assert batch.snapshot(prepared[0])["rows"][key]["run_id"] is None


@pytest.mark.parametrize("relative", [
    "solver.py", "plan-revisions/1.json", "inputs/input.inp", "environment.json",
    "request-revisions/2.json",
])
def test_binding_rejects_unknown_files_in_a_real_initial_run(prepared, relative):
    key, manifest = workspace(prepared)
    store, run = create_shell(manifest)
    path = store.path(f"runs/{run.id}/{relative}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{}", encoding="utf-8")
    with pytest.raises(ValueError, match="unknown files|initial Request"):
        batch.bind_run(prepared[0], key, run.id)


@pytest.mark.parametrize("change", [
    "source", "update", "other_text", "second_message", "invalid_control", "timestamp",
])
def test_binding_rejects_sources_and_authority_or_task_changes_in_initial_queue(prepared, change):
    key, manifest = workspace(prepared)
    store, run = create_shell(manifest)
    original = store.load_request(run).original_text
    if change == "source":
        store._write_json(f"runs/{run.id}/sources.json", {"old_task": {"files": []}})
    elif change == "invalid_control":
        store._write_json(f"runs/{run.id}/control.json", [])
    else:
        store.enqueue_message(run.id, original if change != "other_text" else "Use my solution script.",
                              update={"budget_amendment": {}} if change == "update" else None)
        if change == "second_message":
            store.enqueue_message(run.id, "Here is an answer from the previous task.")
        elif change == "timestamp":
            path = store.path(f"runs/{run.id}/control.json")
            value = json.loads(path.read_text(encoding="utf-8"))
            value["messages"][0]["created_at"] = "Prewritten solver text hidden in metadata."
            path.write_text(json.dumps(value), encoding="utf-8")
    with pytest.raises(ValueError, match="fresh Run"):
        batch.bind_run(prepared[0], key, run.id)


def test_concurrent_duplicate_reservations_count_once(prepared):
    with ThreadPoolExecutor(max_workers=6) as workers:
        answers = list(workers.map(lambda _: reserve(prepared[0]), range(12)))
    assert len(set(answers)) == 1
    assert len(batch.snapshot(prepared[0])["rows"]) == 1


@pytest.mark.parametrize("publication", ["reservation", "settlement"])
def test_interrupted_publication_preserves_receipt_and_refuses_restart(prepared, monkeypatch, publication):
    root = prepared[0]
    key = reserve(root) if publication == "settlement" else batch.slot_id("T1", 1, "formal")
    original = batch.atomic_write

    def fail_ledger(path, data, **kwargs):
        if path.name == "ledger.json":
            raise OSError("synthetic interrupted ledger publication")
        return original(path, data, **kwargs)

    monkeypatch.setattr(batch, "atomic_write", fail_ledger)
    with pytest.raises(OSError, match="interrupted"):
        if publication == "reservation":
            reserve(root)
        else:
            batch.settle_slot(root, key, known_usage=dict.fromkeys(batch.METRICS, 0), unknown_fields={})
    assert (root / "receipts" / key / (publication + ".json")).exists()
    with pytest.raises(ValueError, match="orphan"):
        batch.snapshot(root)
    with pytest.raises(ValueError, match="orphan"):
        reserve(root)


def test_ledger_rollback_and_immutable_reservation_tampering_are_detected(prepared):
    root = prepared[0]
    key = reserve(root)
    path = root / "receipts" / key / "reservation.json"
    value = json.loads(path.read_text(encoding="utf-8"))
    value["reservation"]["model_tokens"] = 0
    path.write_text(json.dumps(value), encoding="utf-8")
    with pytest.raises(ValueError, match="immutable reservation differs"):
        batch.snapshot(root)


def test_workspaces_contain_only_frozen_text_config_budget_and_share_environment(prepared):
    root, environment, value, config, texts = prepared
    first_key, first = workspace(prepared)
    second_key, second = workspace(prepared, 1)
    assert first["data_root"] != second["data_root"]
    assert first["environment_root"] == second["environment_root"] == str(environment)
    for key, manifest in ((first_key, first), (second_key, second)):
        directory = root / "workspaces" / key
        assert {path.name for path in directory.iterdir()} == {
            "original.txt", "config.json", "run-budget.json", "workspace.json", "data"}
        assert not list(Path(manifest["data_root"]).iterdir())
        derived = json.loads((directory / "config.json").read_text(encoding="utf-8"))
        expected = config.model_dump(mode="json")
        expected["data_root"] = manifest["data_root"]
        expected["text"]["budget"] = budget()
        assert derived == expected
        assert derived["text"]["permission"] == config.text.permission.model_dump(mode="json")
    # Stores retain distinct data roots and use the exact same environment lock.
    stores = [Store(item["data_root"], environment_root=item["environment_root"]) for item in (first, second)]
    assert stores[0].root != stores[1].root
    assert stores[0]._environment_lock().lock_file == stores[1]._environment_lock().lock_file
    assert batch.snapshot(root)["rows"][first_key]["workspace"] == first


@pytest.mark.parametrize("change", ["text", "permission", "model", "run_budget", "extra_starts"])
def test_workspace_cannot_change_frozen_task_configuration_or_reservation(prepared, change):
    root, _, _, config, texts = prepared
    key = reserve(root)
    supplied, limits, text = config.model_dump(mode="json"), budget(), texts["T1"]
    if change == "text":
        text += " Substitute a different goal."
    elif change == "permission":
        supplied["text"]["permission"]["code_execution"] = True
    elif change == "model":
        supplied["model"]["output_tokens"] += 1
    elif change == "run_budget":
        limits["model_tokens"] += 1
    else:
        limits["extra_orca_starts"] = limits["orca_starts"] + 1
    with pytest.raises(ValueError, match="frozen|reservation|envelope"):
        batch.prepare_workspace(root, key, original_text=text, configuration=supplied, run_budget=limits)
    assert not (root / "workspaces" / key).exists()


def test_existing_workspace_cannot_supply_old_scripts_or_be_overwritten(prepared):
    root, _, _, config, texts = prepared
    key = reserve(root)
    directory = root / "workspaces" / key
    directory.mkdir(parents=True)
    (directory / "solution.py").write_text("previous task solver", encoding="utf-8")
    with pytest.raises(FileExistsError):
        batch.prepare_workspace(root, key, original_text=texts["T1"],
                                configuration=config.model_dump(mode="json"), run_budget=budget())
    assert (directory / "solution.py").read_text(encoding="utf-8") == "previous task solver"


@pytest.mark.parametrize("change", ["config", "original", "injected_script"])
def test_prepared_workspace_inputs_are_verified_when_reopened(prepared, change):
    key, _ = workspace(prepared)
    directory = prepared[0] / "workspaces" / key
    if change == "config":
        (directory / "config.json").write_text("{}", encoding="utf-8")
    elif change == "original":
        (directory / "original.txt").write_text("new goal", encoding="utf-8")
    else:
        (directory / "solver.py").write_text("old solution", encoding="utf-8")
    with pytest.raises(ValueError, match="changed|injected"):
        batch.snapshot(prepared[0])


def test_symlink_workspace_is_rejected(prepared, tmp_path):
    root, _, _, config, texts = prepared
    key = reserve(root)
    outside = tmp_path / "outside"
    outside.mkdir()
    directory = root / "workspaces" / key
    directory.parent.mkdir()
    try:
        directory.symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("OS does not permit creating the real symlink fixture")
    with pytest.raises(ValueError, match="links"):
        batch.prepare_workspace(root, key, original_text=texts["T1"],
                                configuration=config.model_dump(mode="json"), run_budget=budget())
    assert not list(outside.iterdir())


@pytest.mark.parametrize("change", ["rows", "duplicate", "unknown_category", "negative", "extra_field",
                                     "missing_budget", "partial_budget", "over_budget", "coerced_budget"])
def test_policy_rows_schema_and_usage_are_bounded(tmp_path, change):
    value, _, _ = policy()
    if change == "rows":
        value["limits"]["rows"] = batch.MAX_ROWS + 1
    elif change == "duplicate":
        value["slots"][1] = deepcopy(value["slots"][0])
    elif change == "unknown_category":
        value["slots"][0]["category"] = "retry_extra"
    elif change == "negative":
        value["limits"]["model_tokens"] = -1
    elif change == "extra_field":
        value["slots"][0]["solution_script"] = "print('not allowed')"
    elif change == "missing_budget":
        value["slots"][0].pop("run_budget")
    elif change == "partial_budget":
        value["slots"][0]["run_budget"].pop("code_retries")
    elif change == "over_budget":
        value["slots"][0]["run_budget"]["orca_starts"] += 1
    else:
        value["slots"][0]["run_budget"]["attempts_per_step"] = True
    with pytest.raises(ValueError):
        batch.create_batch(tmp_path / "batch", freeze_sha256="a" * 64, policy=value,
                           environment_root=tmp_path / "environment")


@pytest.mark.parametrize("field,value", [
    ("attempts_per_step", 100), ("code_retries", 50), ("decision_rounds", 1000),
    ("transport_retries", 10), ("plan_revisions", 30), ("analysis_executions", 100),
    ("artifact_total_bytes", 1_000_000_000), ("evidence_reads", 500),
])
def test_non_accounting_run_limits_cannot_change_between_task_repetitions(prepared, field, value):
    root, _, _, config, texts = prepared
    key = reserve(root)
    changed = budget(**{field: value})
    with pytest.raises(ValueError, match="complete frozen slot budget"):
        batch.prepare_workspace(root, key, original_text=texts["T1"],
            configuration=config.model_dump(mode="json"), run_budget=changed)
    assert not (root / "workspaces" / key).exists()


def test_workspace_rejects_partial_budget_even_when_missing_values_would_default(prepared):
    root, _, _, config, texts = prepared
    key = reserve(root)
    partial = budget()
    partial.pop("code_retries")
    with pytest.raises(ValueError, match="complete frozen BudgetLimits"):
        batch.prepare_workspace(root, key, original_text=texts["T1"],
            configuration=config.model_dump(mode="json"), run_budget=partial)


def test_reference_has_its_own_frozen_thirty_start_budget_not_formal_twelve(tmp_path):
    config = Config()
    config.text.budget = BudgetLimits(orca_starts=12, extra_orca_starts=11,
        model_calls=24, model_tokens=1_200_000, internal_work_units=10000, run_seconds=7200,
        file_writes=200)
    reference_budget = config.text.budget.model_copy(update={"orca_starts": 30,
        "extra_orca_starts": 29, "model_calls": 0, "model_tokens": 0,
        "internal_work_units": 30000, "run_seconds": 21600.0}).model_dump(mode="json")
    value, _, texts = policy(count=1)
    slot = value["slots"][0]
    slot.update(category="reference", run_budget=reference_budget,
                reservation=dict(model_calls=0, model_tokens=0, orca_starts=30,
                                 internal_work_units=30000, wall_seconds=21600))
    value["configuration_sha256"] = batch.canonical_sha256(config.model_dump(mode="json"))
    value["limits"] = {"rows": 1, **slot["reservation"]}
    root = tmp_path / "batch"
    batch.create_batch(root, freeze_sha256="a" * 64, policy=value, environment_root=tmp_path / "environment")
    key = reserve(root, value=value)
    result = batch.prepare_workspace(root, key, original_text=texts["T1"],
        configuration=config.model_dump(mode="json"), run_budget=reference_budget)
    actual = json.loads((Path(result["data_root"]).parent / "config.json").read_text(encoding="utf-8"))
    assert actual["text"]["budget"] == reference_budget
    assert actual["text"]["permission"] == config.text.permission.model_dump(mode="json")
    assert batch.snapshot(root)["rows"][key]["slot"]["run_budget"] == reference_budget
