"""Offline observer tests; no model, generated execution, Docker or ORCA."""

import hashlib
import importlib.util
import json
from pathlib import Path

import pytest

from orca_agent.llm import ModelReply, ModelUsage, prepare_request
from orca_agent.model_usage import current_basis, send_model
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


def frozen_case(tmp_path, monkeypatch, *, content=None, declaration=True, selection="explicit",
                predecessor=False):
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
    run = store.create_run(request, plan, PermissionSnapshot(model_execution=True,
        code_execution=True, artifact_writes=True,
        allowed_tools=["workspace.write", "python.execute"]),
        BudgetLimits(analysis_executions=3, plan_revisions=2, code_retries=1,
            model_calls=2, model_tokens=10000, input_tokens=12000, output_tokens=1000))
    result = execute_call(store, run, writer.tool, writer.parameters.model_dump(), step=writer)
    artifact = result.observations["written_artifact"]
    code = {key: artifact[key] for key in ("artifact_id", "sha256")}
    step = Step(id="execute", logical_id="execute", tool="python.execute",
        parameters={"code": code, "observation_names": ["artifacts"]})
    pending = [Step(id="pending_writer", logical_id="pending_writer", tool="workspace.write",
        parameters={"name": "pending.txt", "content": "pending", "role": "text"})] if predecessor else []
    plan = plan.model_copy(deep=True, update={"version": 2, "steps": [writer, *pending, step],
        "goal_map": {"report": OutputBinding(port="artifacts", step_id=step.id)}})
    decision_id = "plan_decision"
    if selection == "committed":
        from test_model_usage import ScriptedTransport
        prepared = prepare_request([{"role": "user", "content": "Offline JSON Plan receipt."}],
                                   max_output_tokens=1000)
        proposal = {"action": "revise_plan", "parameters": {
            "steps": [{"key": writer.id}, *[{"key": item.id, "tool": item.tool,
                "parameters": item.parameters.model_dump(mode="json")} for item in [*pending, step]]],
            "goal_map": {"report": {"step_key": step.id, "port": "artifacts"}}},
            "reason": "Synthetic accepted Plan, no external model."}
        send_model(store, run, prepared, ScriptedTransport(ModelReply(request_hash=prepared.request_hash,
            proposal=proposal, usage=ModelUsage(10, 5, 15))), basis=current_basis(store, run),
            logical_id="offline_plan")
        decision_id = run.model_records[-1]["id"]
    run = store.commit_revision(run, plan, decision_id=decision_id,
        basis=current_basis(store, run), related_results=[result.id])
    if selection == "explicit":
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
    assert receipt["selection_origin"] == "unapplied_call_tool"
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
        run.applied_decisions.append("execute_decision")
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


def test_accepted_model_plan_selects_first_ready_without_extra_call_or_mutation(tmp_path, monkeypatch):
    store, run, step, _, gate = frozen_case(tmp_path, monkeypatch, selection="committed")
    before = store.load_run(run.id).model_dump_json()
    proof = DRIVER.inspect_seeded_execution(store, run.id)
    assert proof["selection_origin"] == "activated_model_plan_first_ready"
    assert proof["step_id"] == step.id and proof["decision_id"] == run.decisions[-1]["id"]
    assert proof["source_model_response_sha256"] == run.model_records[-1]["response_record_sha256"]
    assert proof["source_decision_record_sha256"] == run.decisions[-1]["record_sha256"]
    assert proof["source_decision_basis"]["plan_version"] == 1 and proof["plan_version"] == 2
    assert not run.attempts and not run.internal_calls
    gate("before_science_reservation")
    assert gate.receipt["status"] == "prelaunch_verified"
    assert store.load_run(run.id).model_dump_json() == before


@pytest.mark.parametrize("selection", ["explicit", "committed"])
@pytest.mark.parametrize("change", ["feedback", "message", "signal", "stale_control"])
def test_selection_cannot_bypass_current_feedback_or_user_control(tmp_path, monkeypatch, selection, change):
    store, run, _, _, gate = frozen_case(tmp_path, monkeypatch, selection=selection)
    if change == "message":
        store.enqueue_message(run.id, "Stop and inspect this new condition.")
    elif change == "signal":
        store.signal(run.id, "pause")
    else:
        altered = run.model_copy(deep=True)
        if change == "feedback":
            altered.processed_feedback = []
        else:
            altered.decisions[-1]["basis"]["control_generation"] -= 1
        original_load = store.load_run
        monkeypatch.setattr(store, "load_run", lambda identifier: altered if identifier == run.id
                            else original_load(identifier))
    with pytest.raises(RuntimeError, match="not_established"):
        gate("before_science_reservation")


def test_accepted_plan_does_not_skip_ready_non_python_step(tmp_path, monkeypatch):
    store, run, step, _, gate = frozen_case(tmp_path, monkeypatch, selection="committed", predecessor=True)
    from orca_agent.agent import _ready
    from orca_agent.runner import _step_results
    assert [item.id for item in _ready(store.load_plan(run), _step_results(store, run))] == [
        "pending_writer", step.id]
    with pytest.raises(RuntimeError, match="ready Python Step"):
        gate("before_science_reservation")
    assert not store.load_run(run.id).attempts


def test_explicit_ready_selection_is_not_replaced_by_first_ready(tmp_path, monkeypatch):
    store, run, step, _, _ = frozen_case(tmp_path, monkeypatch, predecessor=True)
    proof = DRIVER.inspect_seeded_execution(store, run.id)
    assert proof["selection_origin"] == "unapplied_call_tool" and proof["step_id"] == step.id


def test_explicit_settled_selection_cannot_be_executed_again(tmp_path, monkeypatch):
    store, run, _, _, gate = frozen_case(tmp_path, monkeypatch)
    altered = run.model_copy(deep=True)
    altered.decisions[-1]["parameters"]["step_id"] = "writer"
    monkeypatch.setattr(store, "load_run", lambda _: altered)
    with pytest.raises(RuntimeError, match="ready Python Step"):
        gate("before_science_reservation")


@pytest.mark.parametrize("change", ["no_model", "wrong_action", "response_hash", "request_hash",
    "model_basis", "decision_payload", "later_rejection", "already_applied"])
def test_selection_requires_exact_current_model_and_commit_provenance(tmp_path, monkeypatch, change):
    selection = "explicit" if change == "already_applied" else "committed"
    store, run, _, _, gate = frozen_case(tmp_path, monkeypatch, selection=selection)
    altered = run.model_copy(deep=True)
    if change in {"response_hash", "request_hash", "decision_payload"}:
        ticket = run.decisions[-1]["id"]
        name = (f"decisions/{ticket}.json" if change == "decision_payload" else
                f"model/{ticket}.{'response' if change == 'response_hash' else 'request'}.json")
        path = store.path(f"runs/{run.id}/{name}")
        path.write_bytes(path.read_bytes() + b" ")
        if change == "decision_payload":
            data = json.loads(path.read_text())
            data["plan"]["steps"][-1]["parameters"]["observation_names"] = ["changed"]
            path.write_text(json.dumps(data))
    elif change == "no_model":
        altered.model_records = []
    elif change == "model_basis":
        altered.model_records[-1]["basis"]["plan_version"] = 0
    elif change == "wrong_action":
        from orca_agent import model_usage
        original = model_usage.read_model_reply
        def wrong_action(*args):
            from dataclasses import replace
            reply, receipt_hash = original(*args)
            return replace(reply, proposal={"action": "stop"}), receipt_hash
        monkeypatch.setattr(model_usage, "read_model_reply", wrong_action)
    elif change == "later_rejection":
        altered.decisions.append({"id": "rejected", "action": "rejected"})
    else:
        altered.applied_decisions.append("execute_decision")
    original_load = store.load_run
    monkeypatch.setattr(store, "load_run", lambda identifier: altered if identifier == run.id
                        else original_load(identifier))
    with pytest.raises(RuntimeError, match="not_established"):
        gate("before_science_reservation")


def test_real_live021_committed_plan_replay_is_read_only(tmp_path):
    root = Path(__file__).parents[2] / "data/code-tools-step3/live-store"
    run_id = "run_6621c9486fe04c2b92ba23858ccb24e5"
    directory = root / "runs" / run_id
    if not directory.is_dir():
        pytest.skip("optional immutable live021 evidence is not installed")
    def hashes():
        return {str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest()
                for path in sorted(directory.rglob("*")) if path.is_file()}
    before = hashes()
    store = Store(root, environment_root=tmp_path / "unused-environment")
    run = store.load_run(run_id)
    assert run.state == "failed" and run.usage.orca_starts_actual == 0 and not run.attempts
    proof = DRIVER.inspect_seeded_execution(store, run_id)
    assert proof["selection_origin"] == "activated_model_plan_first_ready"
    assert proof["decision_id"] == "model_0517bdc185294ec9b6f09c4d305d190b"
    assert proof["source_model_response_sha256"] == (
        "74ee80be11843e2649ab74e2ca4c8de5a4d15aef8be8bd2a66209c6e549745b4")
    assert proof["syntax"]["line"] == 1
    assert hashes() == before and store.load_run(run_id) == run
    assert not (directory / "seeded-code-error-prelaunch.json").exists()


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
