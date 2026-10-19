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


def extended_v2_batch(tmp_path, count=8):
    path = original_batch(tmp_path)
    DRIVER.extend_development_budget_once(path, "The historical five-to-eight authorization.")
    for index in range(6, count + 1):
        DRIVER.reserve_development(tmp_path / f"live-{index:03d}", configuration())
    return path


def test_fifty_run_revision_preserves_both_prior_histories_and_per_run_envelopes(tmp_path):
    path = extended_v2_batch(tmp_path)
    raw, before = path.read_bytes(), load(path)
    revision = DRIVER.extend_development_budget_to_fifty(path, "2026-10-19 user authorized 50 total Runs.")
    after = load(path)
    assert after["version"] == 3
    assert after["limits"] == {
        "runs": 50, "model_calls": 1200, "model_tokens": 60000000,
        "orca_starts": 100, "internal_work_units": 20000}
    assert after["reservations"] == before["reservations"]
    assert after["meaning"] == before["meaning"]
    assert after["separate_native_probe_starts"] == before["separate_native_probe_starts"] == 1
    assert after["revisions"] == [before["revisions"][0], revision]
    assert revision["prior_ledger_sha256"] == hashlib.sha256(raw).hexdigest()
    assert revision["prior_revisions_sha256"] == DRIVER._canonical_hash(before["revisions"])
    assert revision["original_reservations_sha256"] == DRIVER._canonical_hash(before["reservations"])
    assert revision["original_reservations_count"] == 8
    assert revision["old_limits"] == before["limits"]
    assert revision["new_limits"] == after["limits"]
    assert DRIVER._valid_timestamp(revision["revised_at"])
    assert DRIVER._validate_development_ledger(before) == DRIVER.DEVELOPMENT_LIMITS_V2
    assert DRIVER._validate_development_ledger(after) == DRIVER.DEVELOPMENT_LIMITS_V3


def test_fifty_is_total_not_fifty_additional_and_never_refunds_failed_reservations(tmp_path):
    path = extended_v2_batch(tmp_path)
    raw = path.read_bytes()
    with pytest.raises(RuntimeError, match="automatic reservation cannot extend"):
        DRIVER.reserve_development(tmp_path / "live-009", configuration())
    assert path.read_bytes() == raw
    DRIVER.extend_development_budget_to_fifty(path, "User explicitly increased total to fifty.")
    original = load(path)
    for index in range(9, 51):
        DRIVER.reserve_development(tmp_path / f"live-{index:03d}", configuration())
    final = load(path)
    assert len(final["reservations"]) == 50
    assert final["reservations"][:8] == original["reservations"]
    assert final["revisions"] == original["revisions"]
    for key in set(final["limits"]) - {"runs"}:
        assert sum(row["budget"][key] for row in final["reservations"]) == final["limits"][key]
    raw = path.read_bytes()
    with pytest.raises(RuntimeError, match="automatic reservation cannot extend"):
        DRIVER.reserve_development(tmp_path / "renamed-extra-run", configuration())
    with pytest.raises(RuntimeError, match="cannot be repeated"):
        DRIVER.extend_development_budget_to_fifty(path, "An attempted third extension.")
    assert path.read_bytes() == raw


@pytest.mark.parametrize("count", [5, 6, 7])
def test_fifty_run_extension_requires_exactly_eight_v2_records(tmp_path, count):
    path = extended_v2_batch(tmp_path, count)
    raw = path.read_bytes()
    with pytest.raises(RuntimeError, match="exactly eight v2"):
        DRIVER.extend_development_budget_to_fifty(path, "Too early.")
    assert path.read_bytes() == raw


def test_fifty_run_extension_cannot_skip_v1_history_or_repeat_either_revision(tmp_path):
    path = original_batch(tmp_path)
    raw = path.read_bytes()
    with pytest.raises(RuntimeError, match="exactly eight v2"):
        DRIVER.extend_development_budget_to_fifty(path, "Cannot bypass historical v2.")
    assert path.read_bytes() == raw
    DRIVER.extend_development_budget_once(path, "Historical first revision.")
    for index in range(6, 9):
        DRIVER.reserve_development(tmp_path / f"live-{index:03d}", configuration())
    DRIVER.extend_development_budget_to_fifty(path, "User's second explicit authorization.")
    raw = path.read_bytes()
    for revise in (DRIVER.extend_development_budget_once, DRIVER.extend_development_budget_to_fifty):
        with pytest.raises(RuntimeError, match="cannot be repeated"):
            revise(path, "Repeated request must not add more capacity.")
    assert path.read_bytes() == raw


def test_fifty_run_extension_never_creates_missing_history(tmp_path):
    path = tmp_path / "development-ledger.json"
    with pytest.raises(FileNotFoundError):
        DRIVER.extend_development_budget_to_fifty(path, "No history available.")
    assert not path.exists()


@pytest.mark.parametrize("reason", [None, "", "  ", "x" * 2001])
def test_fifty_run_extension_requires_explicit_reason(tmp_path, reason):
    path = extended_v2_batch(tmp_path)
    raw = path.read_bytes()
    with pytest.raises(RuntimeError, match="explicit reason"):
        DRIVER.extend_development_budget_to_fifty(path, reason)
    assert path.read_bytes() == raw


@pytest.mark.parametrize("key", ["model_calls", "model_tokens", "orca_starts", "internal_work_units"])
def test_fifty_run_batch_does_not_expand_the_per_run_cap(tmp_path, key):
    path = extended_v2_batch(tmp_path)
    DRIVER.extend_development_budget_to_fifty(path, "Keep the original per-Run envelope.")
    raw = path.read_bytes()
    with pytest.raises(RuntimeError, match="invalid development reservation budget"):
        DRIVER.reserve_development(tmp_path / "live-009", configuration(**{
            key: DRIVER.DEVELOPMENT_RUN_LIMITS[key] + 1}))
    assert path.read_bytes() == raw
    DRIVER.reserve_development(tmp_path / "live-009", configuration())
    assert len(load(path)["reservations"]) == 9


@pytest.mark.parametrize("change", ["first_revision_reason", "first_revision_raw_hash",
    "first_five_reservations", "later_three_reservations", "deleted_reservation", "deleted_revision",
    "extra_revision", "v3_limits", "v3_new_limits", "v3_old_limits", "v3_count", "v3_prefix_hash",
    "v3_prior_revisions_hash", "v3_raw_hash", "v3_action", "new_row_budget"])
def test_fifty_run_reservation_rejects_altered_history_or_limits_without_writing(tmp_path, change):
    path = extended_v2_batch(tmp_path)
    DRIVER.extend_development_budget_to_fifty(path, "Preserve history.")
    DRIVER.reserve_development(tmp_path / "live-009", configuration())
    ledger = load(path)
    revision = ledger["revisions"][-1]
    if change == "first_revision_reason":
        ledger["revisions"][0]["reason"] = "Replaced old authorization."
    elif change == "first_revision_raw_hash":
        ledger["revisions"][0]["prior_ledger_sha256"] = "0" * 64
    elif change == "first_five_reservations":
        ledger["reservations"][0]["budget"]["model_calls"] = 0
    elif change == "later_three_reservations":
        ledger["reservations"][7]["budget"]["model_calls"] = 0
    elif change == "deleted_reservation":
        ledger["reservations"].pop(0)
    elif change == "deleted_revision":
        ledger["revisions"].pop(0)
    elif change == "extra_revision":
        ledger["revisions"].append(copy.deepcopy(revision))
    elif change == "v3_limits":
        ledger["limits"]["runs"] = 58
    elif change == "v3_new_limits":
        revision["new_limits"]["runs"] = 58
    elif change == "v3_old_limits":
        revision["old_limits"]["runs"] = 8.0
    elif change == "v3_count":
        revision["original_reservations_count"] = 8.0
    elif change == "v3_prefix_hash":
        revision["original_reservations_sha256"] = "0" * 64
    elif change == "v3_prior_revisions_hash":
        revision["prior_revisions_sha256"] = "0" * 64
    elif change == "v3_raw_hash":
        revision["prior_ledger_sha256"] = "not a hash"
    elif change == "v3_action":
        revision["action"] = "automatic_expansion"
    elif change == "new_row_budget":
        ledger["reservations"][-1]["budget"]["model_calls"] += 1
    path.write_text(json.dumps(ledger), encoding="utf-8")
    raw = path.read_bytes()
    with pytest.raises(RuntimeError):
        DRIVER.reserve_development(tmp_path / "live-010", configuration())
    assert path.read_bytes() == raw


def test_fifty_run_extension_refuses_damaged_v2_history_without_writing(tmp_path):
    path = extended_v2_batch(tmp_path)
    ledger = load(path)
    ledger["reservations"][0]["budget"]["model_tokens"] = 0
    path.write_text(json.dumps(ledger), encoding="utf-8")
    raw = path.read_bytes()
    with pytest.raises(RuntimeError, match="altered original reservations"):
        DRIVER.extend_development_budget_to_fifty(path, "Cannot bless damaged history.")
    assert path.read_bytes() == raw


def test_fifty_run_cli_only_migrates_the_ledger(tmp_path, monkeypatch, capsys):
    path = extended_v2_batch(tmp_path)
    def forbidden(*args, **kwargs):
        pytest.fail("the administrative fifty-Run revision must not reach execution")
    for name in ("load_config", "create_text", "execute", "Store", "SeededCodeErrorGate", "reserve_development"):
        monkeypatch.setattr(DRIVER, name, forbidden)
    monkeypatch.setattr(DRIVER.subprocess, "check_output", forbidden)
    assert DRIVER.main(["extend-development-budget-to-fifty", "--ledger", str(path),
                        "--reason", "2026-10-19 user authorization; offline CLI fixture."]) == 0
    receipt = json.loads(capsys.readouterr().out)
    assert receipt["version"] == 3 and receipt["run_created"] is False
    assert len(load(path)["reservations"]) == 8
    assert len(load(path)["revisions"]) == 2
