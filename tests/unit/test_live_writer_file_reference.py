"""Real failed writer selection and its shared name/port provenance boundary."""

import copy
import hashlib
import json
from pathlib import Path

import pytest

from orca_agent.file_references import resolve_step_files
from orca_agent.models import ProducerFileRef
from orca_agent.proposals import materialize_plan
from tests.unit.test_simplified_file_references import _case, _commit, _proposal, _write
from tests.unit.test_simplified_plan_repairs import case, settle

FIXTURE = Path(__file__).parents[1] / "fixtures/code_contracts/live-h1-a004-writer-file-reference.json"
DIGEST = "463563c552da6a428e231e2d2245a2a208133256afc481b33ef3d21c33e10bd3"


def test_actual_a004_repair_accepts_exact_writer_name_and_pins_its_written_input(tmp_path, monkeypatch):
    assert hashlib.sha256(FIXTURE.read_bytes()).hexdigest() == DIGEST
    retained = json.loads(FIXTURE.read_bytes())["proposal"]["parameters"]
    store, run, _, prior, _ = case(tmp_path, monkeypatch, native=True)
    old, _ = settle(store, run, prior.steps[0])
    proposal = copy.deepcopy(retained)
    proposal["steps"][1]["repair_of"] = old.step_id  # Only the local offline identity changes.
    before = copy.deepcopy(proposal)
    run, plan = _commit(store, run, proposal)
    writer, consumer = plan.steps
    assert proposal == before
    assert consumer.logical_id == old.logical_id
    assert isinstance(consumer.parameters.input, ProducerFileRef)
    assert consumer.parameters.input.file_key == writer.parameters.name == "hcn_nmr.inp"
    assert consumer.parameters.input.port is None
    assert consumer.parameters.inputs == prior.steps[0].parameters.inputs
    written = _write(store, run, writer).observations["written_artifact"]
    attempt = store.reserve_attempt(run, consumer)
    concrete = attempt.frozen_step.parameters.input
    source = attempt.consumption["_file_sources"]["input"]
    assert (concrete.artifact_id, concrete.sha256) == (written["artifact_id"], written["sha256"])
    assert source["declaration"] == consumer.parameters.input.model_dump(mode="json")
    assert source["declaration"]["file_key"] == "hcn_nmr.inp"
    assert source["sha256"] == hashlib.sha256(writer.parameters.content.encode()).hexdigest()
    assert store.artifact_path(concrete.artifact_id).read_text(encoding="utf-8") == (
        retained["steps"][0]["parameters"]["content"])
    assert attempt.state == "intent" and not attempt.started
    assert run.usage.model_calls == run.usage.orca_starts_actual == 0


@pytest.mark.parametrize("selector", [{"port": "written_artifact"}, {"file_key": "task.py"}])
def test_writer_name_and_port_resolve_identically_without_changing_the_declaration(tmp_path, selector):
    store, run, _, _ = _case(tmp_path)
    run, plan = _commit(store, run, _proposal(code={"producer_key": "write", **selector}))
    writer, consumer = plan.steps
    result = _write(store, run, writer)
    sources = {}
    resolved = resolve_step_files(store, run, consumer, plan=plan, sources=sources)
    written = result.observations["written_artifact"]
    assert (resolved.parameters.code.artifact_id, resolved.parameters.code.sha256) == (
        written["artifact_id"], written["sha256"])
    assert sources["code"]["declaration"] == consumer.parameters.code.model_dump(mode="json")
    assert not run.attempts and not run.permission.artifact_ids


@pytest.mark.parametrize("name", ["missing.py", "task/task.py", "other/task.py", "../task.py"])
def test_writer_name_never_uses_basename_or_task_relative_fallback(tmp_path, name):
    store, run, _, _ = _case(tmp_path)
    with pytest.raises(ValueError, match="exactly match"):
        materialize_plan(store, run, _proposal(code={"producer_key": "write", "file_key": name}))
    assert not run.calls and not run.attempts and store.load_plan(run) is None


@pytest.mark.parametrize("role", ["orca", "text"])
def test_writer_name_cannot_bypass_the_declared_executable_role(tmp_path, role):
    store, run, _, _ = _case(tmp_path)
    with pytest.raises(ValueError, match="writer role"):
        materialize_plan(store, run, _proposal(
            writer_role=role, code={"producer_key": "write", "file_key": "task.py"}))
    assert not run.calls and not run.attempts


@pytest.mark.parametrize("selector", [{"port": "written_artifact"}, {"file_key": "task.py"}])
@pytest.mark.parametrize("field", ["name", "role"])
def test_writer_result_identity_is_checked_for_both_selector_forms(tmp_path, monkeypatch, selector, field):
    store, run, _, _ = _case(tmp_path)
    run, plan = _commit(store, run, _proposal(code={"producer_key": "write", **selector}))
    result = _write(store, run, plan.steps[0])
    changed = result.model_copy(deep=True)
    changed.observations["written_artifact"][field] = "different"
    original = store.load_result
    monkeypatch.setattr(store, "load_result", lambda run_id, result_id:
        changed if result_id == changed.id else original(run_id, result_id))
    with pytest.raises(ValueError, match="name or role"):
        resolve_step_files(store, run, plan.steps[-1], plan=plan)
    assert not run.attempts


def test_matching_name_from_another_writer_cannot_replace_the_explicit_producer(tmp_path, monkeypatch):
    store, run, _, _ = _case(tmp_path)
    proposal = _proposal(code={"producer_key": "write", "file_key": "task.py"})
    other = copy.deepcopy(proposal["steps"][0])
    other["key"] = "other"
    other["parameters"]["content"] = "print('different writer')\n"
    proposal["steps"].insert(0, other)
    run, plan = _commit(store, run, proposal)
    first, selected, consumer = plan.steps
    other_result = _write(store, run, first)
    selected_result = _write(store, run, selected)
    changed = selected_result.model_copy(deep=True)
    changed.observations["written_artifact"] = other_result.observations["written_artifact"]
    changed.artifact_ids = other_result.artifact_ids
    original = store.load_result
    monkeypatch.setattr(store, "load_result", lambda run_id, result_id:
        changed if result_id == changed.id else original(run_id, result_id))
    with pytest.raises(ValueError, match="producing Call"):
        resolve_step_files(store, run, consumer, plan=plan)
    assert not run.attempts
