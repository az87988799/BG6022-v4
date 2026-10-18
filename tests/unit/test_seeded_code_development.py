"""Offline observer tests; no model, generated execution, Docker or ORCA."""

import importlib.util
import json
from pathlib import Path

import pytest

from orca_agent.model_usage import current_basis
from orca_agent.models import (
    BudgetLimits,
    Goal,
    OutputBinding,
    PermissionSnapshot,
    Plan,
    Request,
    Step,
)
from orca_agent.store import Store
from orca_agent.tools.dispatch import execute_call

SPEC = importlib.util.spec_from_file_location(
    "seeded_code_development", Path(__file__).parents[2] / "scripts/code_tools_development.py")
DRIVER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(DRIVER)
MARKER = DRIVER.SEEDED_MARKER


@pytest.mark.parametrize("prefix", ["", "# comment\n", '"""module docs"""\n',
    '"""module docs"""\nfrom __future__ import annotations\n'])
def test_ast_accepts_only_before_any_work_without_executing(prefix, tmp_path):
    target = tmp_path / "must-not-exist"
    source = prefix + MARKER + "\n" + f"open({str(target)!r}, 'w').write('not executed')\n"
    inspected = DRIVER.inspect_seeded_code(source)
    assert inspected["marker"] == MARKER and inspected["compiled_without_execution"]
    assert not target.exists()


@pytest.mark.parametrize("source", [
    "", "# " + MARKER, repr(MARKER), "pass\n" + MARKER,
    "import os\n" + MARKER, MARKER + "=1\n" + MARKER,
    "if True:\n    " + MARKER, MARKER + "()", "OTHER_UNDEFINED_NAME",
    "from __future__ import annotations as " + MARKER + "\n" + MARKER,
    MARKER + "\nif (", MARKER + "\nfrom __future__ import annotations\n",
])
def test_missing_reordered_bound_or_invalid_marker_is_not_established(source):
    with pytest.raises((ValueError, SyntaxError)):
        DRIVER.inspect_seeded_code(source)


def frozen_case(tmp_path, monkeypatch, *, content=None, declaration=True):
    monkeypatch.setattr("orca_agent.store.require_execution_available", lambda _: None)
    store = Store(tmp_path / "data", environment_root=tmp_path / "environment")
    request = Request(original_text="Original scientific request.\n" + (
        DRIVER.SEEDED_DECLARATION if declaration else "No fault test requested."),
        goals=[Goal(id="report", port="artifacts", minimum_check_version="generic-observation-1",
            evidence_contract_version="generic-observation-1", verification="reported_with_limitations")])
    writer = Step(id="writer", logical_id="writer", tool="workspace.write", parameters={
        "name": "task.py", "content": content if content is not None else MARKER + "\n", "role": "python"})
    plan = Plan(request_id=request.id, steps=[writer],
        goal_map={"report": OutputBinding(port="artifacts", gap="not executed")})
    run = store.create_run(request, plan, PermissionSnapshot(code_execution=True, artifact_writes=True,
        allowed_tools=["workspace.write", "python.execute"]),
        BudgetLimits(analysis_executions=2, plan_revisions=2, code_retries=1))
    result = execute_call(store, run, writer.tool, writer.parameters.model_dump(), step=writer)
    artifact = result.observations["written_artifact"]
    code = {key: artifact[key] for key in ("artifact_id", "sha256")}
    step = Step(id="execute", logical_id="execute", tool="python.execute",
        parameters={"code": code, "observation_names": ["artifacts"]})
    plan = plan.model_copy(deep=True, update={"version": 2, "steps": [writer, step],
        "goal_map": {"report": OutputBinding(port="artifacts", step_id=step.id)}})
    run = store.commit_revision(run, plan, decision_id="plan_decision", basis=current_basis(store, run))
    run.decisions.append({"id": "execute_decision", "action": "call_tool",
        "parameters": {"step_id": step.id}, "basis": current_basis(store, run)})
    store.save_run(run)
    evidence = tmp_path / "evidence"
    evidence.mkdir()
    return store, run, step, evidence, DRIVER.SeededCodeErrorGate(store, run.id, evidence)


def test_gate_reads_frozen_model_selection_and_hash_without_changing_run_or_code(tmp_path, monkeypatch):
    store, run, step, evidence, gate = frozen_case(tmp_path, monkeypatch)
    before = store.load_run(run.id).model_dump_json()
    code = store.artifact_path(step.parameters.code.artifact_id).read_bytes()
    gate("after_model_response_saved")
    assert gate.receipt is None and gate.outcome()["status"] == "not_established"
    gate("before_science_reservation")
    receipt = json.loads((evidence / "seeded-code-error-prelaunch.json").read_text())
    assert receipt["status"] == "prelaunch_verified"
    assert receipt["code"]["sha256"] == step.parameters.code.sha256
    assert receipt["usage_before"]["orca_starts_reserved"] == 0
    assert store.load_run(run.id).model_dump_json() == before
    assert store.artifact_path(step.parameters.code.artifact_id).read_bytes() == code
    saved = (evidence / "seeded-code-error-prelaunch.json").read_bytes()
    gate("before_science_reservation")  # Observer does not seed subsequent repairs.
    assert (evidence / "seeded-code-error-prelaunch.json").read_bytes() == saved


@pytest.mark.parametrize("change", ["no_declaration", "normal_code", "attempt", "selection", "hash"])
def test_gate_stops_unestablished_fault_before_a_new_attempt(tmp_path, monkeypatch, change):
    store, run, step, evidence, gate = frozen_case(tmp_path, monkeypatch,
        content="print('ordinary')\n" if change == "normal_code" else None,
        declaration=change != "no_declaration")
    if change == "attempt":
        store.reserve_attempt(run, step)  # Reservation only, no process.
    elif change == "selection":
        run.decisions.append({"id": "other", "action": "call_tool", "parameters": {"step_id": "writer"}})
        store.save_run(run)
    elif change == "hash":
        store.artifact_path(step.parameters.code.artifact_id).write_bytes(b"changed test fixture")
    before = store.load_run(run.id).model_dump_json()
    with pytest.raises(RuntimeError, match="not_established"):
        gate("before_science_reservation")
    receipt = json.loads((evidence / "seeded-code-error-prelaunch.json").read_text())
    assert receipt["status"] == "not_established" and receipt["mutated_task_content"] is False
    assert store.load_run(run.id).model_dump_json() == before
    with pytest.raises(RuntimeError, match="gate remains closed"):
        gate("before_science_reservation")


@pytest.mark.parametrize("switch,explicit,error", [
    (False, False, None), (True, False, "explicit declaration"), (True, True, None)])
def test_cli_keeps_natural_default_and_checks_seeded_request_before_budget(
        tmp_path, monkeypatch, switch, explicit, error):
    text = tmp_path / "request.txt"
    text.write_text("Original text.\n" + (DRIVER.SEEDED_DECLARATION if explicit else ""))
    evidence = tmp_path / "evidence"
    args = ["driver", "--config", "unused.toml", "--text-file", str(text),
        "--evidence", str(evidence), "--live-model", "--live-orca"]
    if switch:
        args.append("--seeded-code-error")
    monkeypatch.setattr("sys.argv", args)
    reached = []
    def stop_before_configuration(path):
        reached.append(path)
        raise RuntimeError("offline stop before configuration/budget")
    monkeypatch.setattr(DRIVER, "load_config", stop_before_configuration)
    if error:
        with pytest.raises(SystemExit):
            DRIVER.main()
        assert not reached and not evidence.exists()
    else:
        with pytest.raises(RuntimeError, match="offline stop"):
            DRIVER.main()
        assert reached and text.read_text().startswith("Original text.")
