"""No model/task execution: current code admission and immutable legacy policy."""

import copy
import json
from pathlib import Path

import pytest

from orca_agent.config import Config
from orca_agent.model_usage import current_basis
from orca_agent.models import RunConfiguration
from orca_agent.planning import InvalidPythonTask, PlanningError
from orca_agent.proposals import materialize_plan
from tests.unit.test_simplified_file_references import _case, _commit, _proposal

ACTUAL = json.loads((Path(__file__).parents[1] /
    "fixtures/code_contracts/live019-comment-only-code.json").read_text("utf-8"))


def case(tmp_path, policy="nonempty-python-1"):
    store, prior, request, _ = _case(tmp_path)
    run = store.create_run(request, None, prior.permission, prior.budget,
        configuration=prior.configuration.model_copy(update={"task_code_policy": policy}))
    return store, run


@pytest.mark.parametrize("content", ["", '"""Only a module description."""\n',
                                      ACTUAL["writer"]["parameters"]["content"]])
def test_empty_and_actual_live019_comment_scripts_fail_before_write_or_attempt(tmp_path, content):
    store, run = case(tmp_path)
    proposal = _proposal()
    proposal["steps"][0]["parameters"]["content"] = content
    original = copy.deepcopy(proposal)
    before = store.path(f"runs/{run.id}/run.json").read_bytes()
    with pytest.raises(PlanningError) as caught:
        _commit(store, run, proposal)
    assert caught.value.detail["code"] == "empty_python_task"
    assert caught.value.detail["path"] == ["parameters", "steps", 1, "parameters", "code"]
    assert proposal == original and store.path(f"runs/{run.id}/run.json").read_bytes() == before
    assert not run.calls and not run.attempts and store.environment_lease() is None


def test_invalid_syntax_reports_position_without_exposing_source(tmp_path):
    store, run = case(tmp_path)
    proposal = _proposal()
    secret = "never_echo_this_source"
    proposal["steps"][0]["parameters"]["content"] = f"{secret} = (\n"
    with pytest.raises(PlanningError) as caught:
        _commit(store, run, proposal)
    assert caught.value.detail["code"] == "invalid_python_task"
    assert caught.value.detail["line"] == 1
    assert secret not in str(caught.value) + json.dumps(caught.value.detail)
    assert not run.calls and not run.attempts and store.environment_lease() is None


def test_concrete_pinned_empty_code_is_rejected_without_changing_artifact(tmp_path):
    content = b"# existing registered but empty task\n"
    store, prior, request, artifacts = _case(tmp_path, external_files=[("empty.py", content, "code")])
    run = store.create_run(request, None, prior.permission, prior.budget,
        configuration=prior.configuration.model_copy(update={"task_code_policy": "nonempty-python-1"}))
    with pytest.raises(PlanningError, match="empty or contains only"):
        _commit(store, run, _proposal(code={"artifact_id": artifacts[0].id}))
    assert store.artifact_path(artifacts[0].id).read_bytes() == content
    assert not run.calls and not run.attempts and store.environment_lease() is None


@pytest.mark.parametrize("content", ["values = [1, 2, 3]\n", "import json\n", "def transform(x):\n    return x\n"])
def test_legitimate_data_import_and_function_modules_remain_accepted(tmp_path, content):
    store, run = case(tmp_path)
    proposal = _proposal()
    proposal["steps"][0]["parameters"]["content"] = content
    updated, plan = _commit(store, run, proposal)
    assert store.load_plan(updated) == plan
    assert not updated.calls and not updated.attempts and store.environment_lease() is None


def test_comment_only_auxiliary_module_is_not_mistaken_for_entry_code(tmp_path):
    store, run = case(tmp_path)
    proposal = _proposal(inputs=[{"producer_key": "module", "port": "written_artifact",
                                 "target_path": "metadata.py", "purpose": "auxiliary module"}])
    proposal["steps"].insert(0, {"key": "module", "tool": "workspace.write", "parameters": {
        "name": "metadata.py", "role": "python", "content": "# metadata-only module\n"}})
    updated, _ = _commit(store, run, proposal)
    assert not updated.attempts and not updated.calls


def test_missing_legacy_policy_keeps_original_serialization_and_admission(tmp_path):
    assert "task_code_policy" not in Config().model_dump(mode="json")
    assert "task_code_policy" not in RunConfiguration().model_dump(mode="json")
    assert Config(task_code_policy="nonempty-python-1").run_configuration().task_code_policy == "nonempty-python-1"
    store, run = case(tmp_path, None)
    store.configuration = store.configuration.model_copy(update={"task_code_policy": "nonempty-python-1"})
    proposal = _proposal()
    proposal["steps"][0]["parameters"]["content"] = ACTUAL["writer"]["parameters"]["content"]
    plan = materialize_plan(store, run, proposal)
    updated = store.commit_revision(run, plan, basis=current_basis(store, run), decision_id="legacy")
    assert "task_code_policy" not in updated.configuration.model_dump(mode="json")
    assert not updated.attempts and not updated.calls


def test_deferred_managed_empty_code_returns_to_model_before_new_reservation(tmp_path):
    """A synthetic settled producer archives code; no task or real model runs."""
    from test_agent import ScriptedTransport

    from orca_agent import agent, runner
    from orca_agent.models import (
        BudgetLimits,
        Goal,
        ModelLimits,
        PermissionSnapshot,
        Request,
        Result,
    )
    from orca_agent.store import Store
    from orca_agent.tools.dispatch import execute_call

    config = Config(data_root=tmp_path / "data", workflow_policy="simplified-1",
        task_code_policy="nonempty-python-1", mechanical_progression=True,
        model=ModelLimits(input_tokens=64000, output_tokens=3000))
    store = Store(config.data_root, environment_root=tmp_path / "environment",
                  configuration=config.run_configuration())
    request = Request(goals=[Goal(id="report", port="artifacts",
        evidence_contract_version="generic-observation-1", minimum_check_version="generic-observation-1",
        verification="reported_with_limitations")])
    run = store.create_run(request, None, PermissionSnapshot(model_execution=True,
        code_execution=True, artifact_writes=True, allowed_tools=["workspace.write", "python.execute"]),
        BudgetLimits(analysis_executions=2, plan_revisions=2, model_calls=4, input_tokens=64000,
                     output_tokens=3000, model_tokens=300000, decision_rounds=12))
    run.agent_enabled = True
    store.save_run(run)
    proposal = _proposal()
    proposal["steps"][1]["key"] = "produce"
    proposal["steps"].append({"key": "consume", "tool": "python.execute", "parameters": {
        "code": {"producer_key": "produce", "file_key": "task/generated.py"}, "sdk_orca": False}})
    # The future managed output cannot be inspected at plan admission.
    run, plan = _commit(store, run, proposal)
    writer = plan.steps[0]
    written = execute_call(store, run, writer.tool, writer.parameters.model_dump(mode="json"), step=writer)
    producer = store.reserve_attempt(run, plan.steps[1])
    output = tmp_path / "synthetic-generated.py"
    output.write_text("# synthetic archived empty entry point\n", encoding="utf-8")
    artifact = store.import_artifact(output, "raw_evidence", run_id=run.id, attempt_id=producer.id)
    result = Result(run_id=run.id, attempt_id=producer.id, step_id=producer.step_id,
        operation_status="completed", artifact_ids=[artifact.id],
        observations={"evidence_collection_complete": True}, source={"synthetic_fixture": True,
        "files": {"task/generated.py": {"artifact_id": artifact.id, "sha256": artifact.sha256}}})
    store.save_result(result)
    store.finish_attempt(run, producer.id, state="completed", result_id=result.id,
                         started=False, termination_confirmed=True)
    run.processed_feedback = [result.id]
    runner._goals(store, run, plan, runner._step_results(store, run))
    store.save_run(run)
    before = run.model_copy(deep=True)
    originals = {path: path.read_bytes() for path in [store.artifact_path(artifact.id),
        store.path(f"runs/{run.id}/results/{result.id}.json"),
        store.path(f"runs/{run.id}/results/{written.id}.json")]}
    callbacks = []
    with pytest.raises(InvalidPythonTask) as caught:
        store.reserve_attempt(run, plan.steps[-1], before_reserve=callbacks.append)
    assert caught.value.detail["code"] == "empty_python_task"
    assert run == before == store.load_run(run.id) and callbacks == []
    assert store.environment_lease() is None
    # The same known rejection cannot recover an active or newly added execution.
    identity = ([item.id for item in run.attempts], [item.id for item in run.calls], [])
    for pending_state in ("unknown", "completed"):
        blocked = run.model_copy(deep=True)
        pending = producer.model_copy(update={"id": "synthetic_new_attempt", "state": pending_state}, deep=True)
        blocked.attempts.append(pending)
        prior_identity = (identity if pending_state == "completed" else
            ([item.id for item in blocked.attempts], identity[1], []))
        with pytest.raises(InvalidPythonTask) as rejected:
            agent._record_admission_error(store, blocked, plan.steps[-1], None, caught.value, prior_identity)
        assert rejected.value is caught.value and not blocked.diagnostics

    def respond(data):
        error = data["CONTROL"]["validation_error"]
        assert error["category"] == "step_admission_rejected"
        assert error["step_id"] == plan.steps[-1].id
        assert error["requirement"] == caught.value.detail
        return {"action": "stop", "parameters": {"reason": "Recorded empty producer code; implementation needed."}}

    transport = ScriptedTransport(respond)
    ended = agent.execute(store, config, run.id, transport=transport)
    assert not transport.scripts and len(transport.sent) == 1, ended.diagnostics
    assert ended.attempts == before.attempts and ended.calls == before.calls
    assert ended.internal_calls == before.internal_calls == [] and store.environment_lease() is None
    excluded = {"model_calls", "model_tokens_used", "decision_rounds"}
    assert ended.usage.model_dump(exclude=excluded) == before.usage.model_dump(exclude=excluded)
    assert ended.usage.model_calls == 1 and ended.usage.model_tokens_used == 75
    assert ended.usage.decision_rounds == before.usage.decision_rounds + 2
    admissions = [row for row in ended.diagnostics if row["category"] == "step_admission_rejected"]
    assert len(admissions) == 1 and admissions[0]["execution_started"] is False
    assert admissions[0]["decision_id"] in ended.applied_decisions
    assert all(path.read_bytes() == raw for path, raw in originals.items())
