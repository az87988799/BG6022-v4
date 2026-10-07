"""Read-only user scope permits evidence access while suppressing scientific work."""

import pytest

from orca_agent import agent
from orca_agent.config import Config
from orca_agent.natural import initialize_bundle
from orca_agent.report import build_report
from tests.unit.test_agent import ScriptedTransport, initial_proposal
from tests.unit.test_natural import bundle_at, store_at
from tests.unit.test_semantic_control import candidate


@pytest.mark.parametrize("scientific_permission", [False, True])
def test_no_calculation_instruction_still_delivers_existing_field_query(tmp_path, monkeypatch, scientific_permission):
    store = store_at(tmp_path)
    raw = tmp_path / "raw.json"
    raw.write_text('{"a": 3}', encoding="utf-8")
    artifact = store.import_artifact(raw, "synthetic_test_evidence")
    text = "Read field a from the registered file. Do not run calculations."
    run = initialize_bundle(store, Config(), bundle_at(tmp_path, goals=None, text=text,
        artifact_ids=[artifact.id], scientific_execution=scientific_permission,
        allowed_tools=["evidence.value", "orca.sp"]))
    permission = run.permission.model_dump_json()
    monkeypatch.setattr(agent, "_science", lambda *args, **kwargs: pytest.fail("read-only scope reached science"))
    normalize = {"action": "normalize_request", "parameters": candidate(store, run, kind="normalize", goals=[{
        "key": "read", "port": "value_observation", "text_basis": "Read field a from the registered file",
        "query": {"artifact_id": artifact.id, "path": [{"kind": "key", "key": "a"}]}}])}
    transport = ScriptedTransport(normalize, initial_proposal)
    completed = agent.execute(store, Config(), run.id, transport=transport)
    assert completed.state == "completed", completed.diagnostics
    assert completed.goal_status == {"goal_read": "satisfied"}
    assert completed.delivery_status == "complete"
    assert completed.permission.model_dump_json() == permission
    assert completed.usage.evidence_reads == 1
    assert completed.usage.orca_starts_actual == completed.usage.orca_starts_reserved == 0
    assert not completed.attempts
    request = store.load_request(completed)
    assert request.method is request.basis is request.charge is request.multiplicity is None
    assert request.unresolved == request.goals[0].unresolved == []
    assert store.active_clarification(completed) is None
    result = store.load_result(completed.id, completed.result_ids[0])
    assert result.observations["value_observation"]["value"] == 3
    assert not result.qualified_outputs
    assert build_report(store, completed)["user_goal_complete"] is True
    for _ in range(2):
        restored = agent.execute(store, Config(), run.id, resume=True, transport=ScriptedTransport())
        assert restored.state == "completed"
        assert restored.usage == completed.usage and restored.result_ids == completed.result_ids


def test_no_execution_overrides_existing_scientific_permission_across_resume(tmp_path, monkeypatch):
    store = store_at(tmp_path)
    (tmp_path / "water.xyz").write_text("3\nSynthetic input\nO 0 0 0\nH 0 .7 .5\nH 0 -.7 .5\n")
    text = "Calculate water electronic energy using RHF/STO-3G for neutral singlet in gas phase. Do not run calculations."
    run = initialize_bundle(store, Config(), bundle_at(tmp_path, goals=None, text=text,
        geometries=[{"id": "water", "file": "water.xyz"}], scientific_execution=True,
        allowed_tools=["orca.sp"]))
    permission = run.permission.model_dump_json()
    monkeypatch.setattr(agent, "_science", lambda *args, **kwargs: pytest.fail("no-execution scope reached science"))
    fields = {"method": ("HF", "RHF"), "basis": ("STO-3G", "STO-3G"), "charge": (0, "neutral"),
              "multiplicity": (1, "singlet"), "electronic_state": ("RHF", "RHF"),
              "environment": ("gas_phase", "gas phase")}
    normalize = {"action": "normalize_request", "parameters": candidate(store, run, kind="normalize",
        conditions={name: {"value": value, "source": "explicit", "text_basis": quote}
                    for name, (value, quote) in fields.items()}, goals=[{
            "key": "energy", "port": "energy", "text_basis": "Calculate water electronic energy",
            "system_refs": ["water"], "geometry_relation": "fixed_initial"}],
        notices=["The electronic-energy request is retained; calculations are disabled by the user."])}
    stopped = agent.execute(store, Config(), run.id, transport=ScriptedTransport(normalize))
    assert stopped.state in {"paused", "waiting_user"}, stopped.diagnostics
    assert stopped.permission.model_dump_json() == permission
    assert not stopped.attempts and not stopped.calls and not stopped.result_ids
    assert stopped.usage.orca_starts_actual == stopped.usage.orca_starts_reserved == 0
    assert stopped.goal_status["goal_energy"] != "satisfied"
    for _ in range(2):
        restored = agent.execute(store, Config(), run.id, resume=True, transport=ScriptedTransport())
        assert restored.state in {"paused", "waiting_user"}
        assert restored.permission.model_dump_json() == permission
        assert restored.usage == stopped.usage
        assert not restored.calls and not restored.attempts and not restored.result_ids
        assert not build_report(store, restored)["user_goal_complete"]
