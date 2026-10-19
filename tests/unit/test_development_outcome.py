"""Development evidence survives report failures; all execution is stubbed offline."""

import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

SPEC = importlib.util.spec_from_file_location(
    "outcome_development", Path(__file__).parents[2] / "scripts/code_tools_development.py")
DRIVER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(DRIVER)


def offline_main_case(tmp_path, monkeypatch, *, execution_error):
    text_file = tmp_path / "request.txt"
    text_file.write_text("Original offline fixture request.\n", encoding="utf-8")
    evidence = tmp_path / "evidence"
    usage = {"model_calls": 3, "model_tokens": 12345, "orca_starts_reserved": 1,
             "orca_starts_actual": 0, "internal_work_units": 25}
    goals = {"report": "unmet", "geometry": "met"}
    final_run = SimpleNamespace(
        id="run_offline_outcome_fixture", state="failed" if execution_error else "completed",
        delivery_status="partial" if execution_error else "complete", goal_status=goals,
        usage=SimpleNamespace(model_dump=lambda **kwargs: dict(usage)))
    initial_run = SimpleNamespace(id=final_run.id)
    calls = {"execute": [], "load_run": [], "reserve": [], "build": [], "render": []}

    def load_run(run_id):
        calls["load_run"].append(run_id)
        return final_run

    store = SimpleNamespace(root=tmp_path / "store", environment_root=tmp_path / "environment",
                            load_run=load_run)
    config = SimpleNamespace(data_root=store.root, run_configuration=lambda: {"offline_fixture": True},
                             model_dump=lambda **kwargs: {"scope": "offline_test_fixture"})

    def execute(actual_store, actual_config, run_id, **kwargs):
        assert actual_store is store and actual_config is config and run_id == initial_run.id
        assert not kwargs
        calls["execute"].append(run_id)
        if execution_error:
            raise RuntimeError("original offline execution failure")
        return final_run

    def build_report(actual_store, run):
        assert actual_store is store and run is final_run
        calls["build"].append(run.id)
        return {"run_id": run.id, "scope": "offline_fixture_report", "goals": goals}

    def render_report(report):
        calls["render"].append(report)
        return "Offline fixture report.\n"

    def make_store(root, *, configuration):
        assert root == config.data_root and configuration == {"offline_fixture": True}
        return store

    def create_text(actual_store, actual_config, text):
        assert actual_store is store and actual_config is config
        assert text == "Original offline fixture request."
        return initial_run, None

    def reserve_development(actual_evidence, actual_config):
        assert actual_evidence == evidence and actual_config is config
        calls["reserve"].append(actual_evidence)

    monkeypatch.setattr(DRIVER, "load_config", lambda _: config)
    monkeypatch.setattr(DRIVER, "Store", make_store)
    monkeypatch.setattr(DRIVER, "create_text", create_text)
    monkeypatch.setattr(DRIVER, "execute", execute)
    monkeypatch.setattr(DRIVER, "reserve_development", reserve_development)
    monkeypatch.setattr(DRIVER, "snapshot", lambda _: {"src/offline-fixture.py": "a" * 64})
    monkeypatch.setattr(DRIVER.subprocess, "check_output", lambda *args, **kwargs: "b" * 40 + "\n")
    monkeypatch.setattr(DRIVER, "build_report", build_report)
    monkeypatch.setattr(DRIVER, "render_report", render_report)
    arguments = ["--config", str(tmp_path / "unused.toml"), "--text-file", str(text_file),
                 "--evidence", str(evidence), "--live-model", "--live-orca"]
    return evidence, final_run, usage, calls, arguments


def assert_retained_outcome(evidence, final_run, usage, calls, *, execution_error):
    outcome = json.loads((evidence / "outcome.json").read_text(encoding="utf-8"))
    assert outcome["run_id"] == final_run.id
    assert outcome["state"] == final_run.state
    assert outcome["delivery_status"] == final_run.delivery_status
    assert outcome["goals"] == final_run.goal_status
    assert outcome["usage"] == usage
    assert outcome["source_changed_during_run"] == []
    assert DRIVER._valid_timestamp(outcome["finished_at"])
    expected_error = ({"type": "RuntimeError", "message": "original offline execution failure"}
                      if execution_error else None)
    assert outcome["error"] == expected_error
    assert calls["execute"] == [final_run.id]
    assert calls["reserve"] == [evidence]
    assert calls["load_run"] == ([final_run.id] if execution_error else [])
    assert (evidence / "freeze.json").is_file()
    assert json.loads((evidence / "run-location.json").read_text(encoding="utf-8"))["run_id"] == final_run.id
    return outcome


@pytest.mark.parametrize("report_failure", ["build", "render"])
@pytest.mark.parametrize("execution_error", [False, True])
def test_report_failure_cannot_erase_original_run_outcome(
        tmp_path, monkeypatch, capsys, report_failure, execution_error):
    evidence, run, usage, calls, arguments = offline_main_case(
        tmp_path, monkeypatch, execution_error=execution_error)

    def fail_report(*args):
        if report_failure == "build":
            raise ValueError("offline report assembly failed")
        raise KeyError("formula")

    monkeypatch.setattr(DRIVER, "build_report" if report_failure == "build" else "render_report", fail_report)
    assert DRIVER.main(arguments) == 2
    outcome = assert_retained_outcome(evidence, run, usage, calls, execution_error=execution_error)
    assert outcome["report_error"] == (
        {"type": "ValueError", "message": "offline report assembly failed"} if report_failure == "build"
        else {"type": "KeyError", "message": "'formula'"})
    # A successfully assembled JSON report is retained even if plain-text rendering fails.
    assert (evidence / "report.json").is_file() is (report_failure == "render")
    assert not (evidence / "report.txt").exists()
    if report_failure == "render":
        assert json.loads((evidence / "report.json").read_text(encoding="utf-8"))["run_id"] == run.id
        assert calls["build"] == [run.id]
    else:
        assert calls["render"] == []
    emitted = json.loads(capsys.readouterr().out.splitlines()[-1])
    assert emitted["error"] == outcome["error"]
    assert emitted["report_error"] == outcome["report_error"]


@pytest.mark.parametrize("execution_error", [False, True])
def test_successful_reporting_keeps_execution_status_and_both_report_files(
        tmp_path, monkeypatch, execution_error):
    evidence, run, usage, calls, arguments = offline_main_case(
        tmp_path, monkeypatch, execution_error=execution_error)
    assert DRIVER.main(arguments) == (2 if execution_error else 0)
    outcome = assert_retained_outcome(evidence, run, usage, calls, execution_error=execution_error)
    assert outcome["report_error"] is None
    report = json.loads((evidence / "report.json").read_text(encoding="utf-8"))
    assert report["run_id"] == run.id
    assert (evidence / "report.txt").read_text(encoding="utf-8") == "Offline fixture report.\n"
    assert calls["build"] == [run.id] and calls["render"] == [report]
