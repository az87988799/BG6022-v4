"""Explicit local development ledger revision; no model or scientific execution."""

import copy
import hashlib
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

SPEC = importlib.util.spec_from_file_location(
    "budget_development", Path(__file__).parents[2] / "scripts/code_tools_development.py")
DRIVER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(DRIVER)


def configuration(**changes):
    budget = dict(model_calls=24, model_tokens=1200000, orca_starts=2, internal_work_units=400)
    budget.update(changes)
    return SimpleNamespace(text=SimpleNamespace(budget=SimpleNamespace(**budget)))


def original_batch(tmp_path, count=5):
    for index in range(count):
        DRIVER.reserve_development(tmp_path / f"live-{index + 1:03d}", configuration())
    return tmp_path / "development-ledger.json"


def load(path):
    return json.loads(path.read_bytes())


def test_explicit_revision_preserves_every_original_reservation_and_prior_raw_hash(tmp_path):
    path = original_batch(tmp_path)
    raw, before = path.read_bytes(), load(path)
    revision = DRIVER.extend_development_budget_once(path, "Three extra bounded development runs authorized.")
    after = load(path)
    assert after["version"] == 2 and after["limits"] == DRIVER.DEVELOPMENT_LIMITS_V2
    assert after["reservations"] == before["reservations"]
    assert after["meaning"] == before["meaning"]
    assert after["separate_native_probe_starts"] == before["separate_native_probe_starts"] == 1
    assert revision == after["revisions"][0]
    assert revision["prior_ledger_sha256"] == hashlib.sha256(raw).hexdigest()
    assert revision["original_reservations_sha256"] == DRIVER._canonical_hash(before["reservations"])
    assert revision["old_limits"] == before["limits"]
    assert revision["new_limits"] == after["limits"] and DRIVER._valid_timestamp(revision["revised_at"])
    assert revision["original_reservations_count"] == 5


def test_ordinary_reservation_never_extends_or_releases_old_failed_unknown_slots(tmp_path):
    path = original_batch(tmp_path)
    raw = path.read_bytes()
    # The ledger does not refund based on any Run's outcome or actual usage.
    with pytest.raises(RuntimeError, match="automatic reservation cannot extend"):
        DRIVER.reserve_development(tmp_path / "renamed-next-run", configuration())
    assert path.read_bytes() == raw
    DRIVER.extend_development_budget_once(path, "Explicit authorization")
    original = load(path)
    for index in range(6, 9):
        DRIVER.reserve_development(tmp_path / f"live-{index:03d}", configuration())
    final = load(path)
    assert final["reservations"][:5] == original["reservations"]
    assert final["limits"] == original["limits"] and final["revisions"] == original["revisions"]
    assert len(final["reservations"]) == 8
    for key in set(final["limits"]) - {"runs"}:
        assert sum(row["budget"][key] for row in final["reservations"]) == final["limits"][key]
    raw = path.read_bytes()
    with pytest.raises(RuntimeError, match="exhausted"):
        DRIVER.reserve_development(tmp_path / "live-009", configuration())
    assert path.read_bytes() == raw
    with pytest.raises(RuntimeError, match="cannot be repeated"):
        DRIVER.extend_development_budget_once(path, "Attempted repeat")
    assert path.read_bytes() == raw


def test_second_extension_is_rejected_immediately_after_first(tmp_path):
    path = original_batch(tmp_path)
    DRIVER.extend_development_budget_once(path, "First explicit authorization")
    raw = path.read_bytes()
    with pytest.raises(RuntimeError, match="cannot be repeated"):
        DRIVER.extend_development_budget_once(path, "Repeat")
    assert path.read_bytes() == raw


@pytest.mark.parametrize("count", [1, 4])
def test_extension_requires_five_existing_reservations(tmp_path, count):
    path = original_batch(tmp_path, count)
    raw = path.read_bytes()
    with pytest.raises(RuntimeError, match="exactly five"):
        DRIVER.extend_development_budget_once(path, "Too early")
    assert path.read_bytes() == raw


def test_extension_never_creates_a_missing_ledger(tmp_path):
    path = tmp_path / "development-ledger.json"
    with pytest.raises(FileNotFoundError):
        DRIVER.extend_development_budget_once(path, "No prior history")
    assert not path.exists()


@pytest.mark.parametrize("change", ["limits", "float_limits", "version", "negative", "boolean", "float_budget",
    "missing_budget", "cumulative", "duplicate", "path", "timestamp", "row_field", "revision", "probe_count"])
def test_invalid_original_ledger_is_preserved_and_rejected(tmp_path, change):
    path = original_batch(tmp_path)
    ledger = load(path)
    row = ledger["reservations"][0]
    if change == "limits":
        ledger["limits"]["model_calls"] += 1
    elif change == "float_limits":
        ledger["limits"]["runs"] = 5.0
    elif change == "version":
        ledger["version"] = True
    elif change == "negative":
        row["budget"]["model_tokens"] = -1
    elif change == "boolean":
        row["budget"]["orca_starts"] = False
    elif change == "float_budget":
        row["budget"]["orca_starts"] = 2.0
    elif change == "missing_budget":
        del row["budget"]["orca_starts"]
    elif change == "cumulative":
        row["budget"]["model_calls"] += 1
    elif change == "duplicate":
        ledger["reservations"][1]["evidence"] = row["evidence"].upper()
    elif change == "path":
        row["evidence"] = "../new"
    elif change == "timestamp":
        row["reserved_at"] = "not a timestamp"
    elif change == "row_field":
        row["reclaimed"] = True
    elif change == "revision":
        ledger["revisions"] = []
    elif change == "probe_count":
        ledger["separate_native_probe_starts"] = True
    path.write_text(json.dumps(ledger), encoding="utf-8")
    raw = path.read_bytes()
    with pytest.raises(RuntimeError):
        DRIVER.extend_development_budget_once(path, "Rejected fixture")
    assert path.read_bytes() == raw
    with pytest.raises(RuntimeError):
        DRIVER.reserve_development(tmp_path / "live-006", configuration())
    assert path.read_bytes() == raw


@pytest.mark.parametrize("change", ["history", "deleted_history", "duplicate_revision", "forged_version",
    "old_limits", "new_limits", "history_hash", "raw_hash"])
def test_reservation_rejects_damaged_revision_or_history(tmp_path, change):
    path = original_batch(tmp_path)
    DRIVER.extend_development_budget_once(path, "Authorized extension")
    ledger = load(path)
    revision = ledger["revisions"][0]
    if change == "history":
        ledger["reservations"][0]["budget"]["model_calls"] = 0
    elif change == "deleted_history":
        ledger["reservations"].pop(0)
    elif change == "duplicate_revision":
        ledger["revisions"].append(copy.deepcopy(revision))
    elif change == "forged_version":
        del ledger["revisions"]
    elif change == "old_limits":
        revision["old_limits"]["runs"] = 5.0
    elif change == "new_limits":
        revision["new_limits"]["runs"] = 9
    elif change == "history_hash":
        revision["original_reservations_sha256"] = "0" * 64
    elif change == "raw_hash":
        revision["prior_ledger_sha256"] = "not a hash"
    path.write_text(json.dumps(ledger), encoding="utf-8")
    raw = path.read_bytes()
    with pytest.raises(RuntimeError):
        DRIVER.reserve_development(tmp_path / "live-006", configuration())
    assert path.read_bytes() == raw


@pytest.mark.parametrize("key,excess", [("model_calls", 73), ("model_tokens", 3600001),
    ("orca_starts", 7), ("internal_work_units", 1201)])
def test_extended_reservation_enforces_each_remaining_cumulative_budget(tmp_path, key, excess):
    path = original_batch(tmp_path)
    DRIVER.extend_development_budget_once(path, "Authorized extension")
    raw = path.read_bytes()
    with pytest.raises(RuntimeError, match="reservation exhausted: " + key):
        DRIVER.reserve_development(tmp_path / "live-006", configuration(**{key: excess}))
    assert path.read_bytes() == raw


@pytest.mark.parametrize("reason", ["", "  ", "x" * 2001])
def test_extension_requires_explicit_reason(tmp_path, reason):
    path = original_batch(tmp_path)
    raw = path.read_bytes()
    with pytest.raises(RuntimeError, match="explicit reason"):
        DRIVER.extend_development_budget_once(path, reason)
    assert path.read_bytes() == raw


def test_duplicate_json_keys_cannot_replace_limits_silently(tmp_path):
    path = original_batch(tmp_path)
    path.write_text(path.read_text().replace('"version": 1,', '"version": 2, "version": 1,'))
    raw = path.read_bytes()
    with pytest.raises(RuntimeError, match="duplicate development ledger JSON key"):
        DRIVER.extend_development_budget_once(path, "Reject duplicate keys")
    assert path.read_bytes() == raw


def test_extension_cli_does_not_load_configuration_create_run_or_execute_anything(tmp_path, monkeypatch, capsys):
    path = original_batch(tmp_path)
    def forbidden(*args, **kwargs):
        pytest.fail("the administrative extension command must not reach execution")
    for name in ("load_config", "create_text", "execute", "Store", "SeededCodeErrorGate", "reserve_development"):
        monkeypatch.setattr(DRIVER, name, forbidden)
    monkeypatch.setattr(DRIVER.subprocess, "check_output", forbidden)
    assert DRIVER.main(["extend-development-budget-once", "--ledger", str(path),
                        "--reason", "Offline test-only explicit revision"]) == 0
    receipt = json.loads(capsys.readouterr().out)
    assert receipt["version"] == 2 and receipt["run_created"] is False
    assert len(load(path)["reservations"]) == 5
