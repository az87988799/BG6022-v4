"""Offline text entry and authenticated identity answers, without live effects."""

import io
import json
from pathlib import Path

import pytest
from test_agent import ScriptedTransport
from test_semantic_control import candidate

from orca_agent import agent, cli, doctor, runner
from orca_agent.config import Config, TextProfile, load_config
from orca_agent.model_usage import current_basis
from orca_agent.models import Goal, OutputBinding, Plan, Step
from orca_agent.natural import agent_budget, bind_text_identity, initialize_text
from orca_agent.semantic import commit_candidate
from orca_agent.store import Store, StoreError
from orca_agent.tools import structure


def text_environment(tmp_path, **profile):
    config = Config(data_root=tmp_path / "data", text=TextProfile(**{
        "enabled": True, "permission": {"model_execution": True, "allowed_tools": []},
        "budget": agent_budget(identity_queries=1, structure_preparations=1), **profile}))
    return Store(config.data_root, environment_root=tmp_path / "environment"), config


def defaults(config):
    return {key: {"value": value, "source": "default", "default_rule": "local-hf-1"}
            for key, value in config.text.defaults.items()}


def normalize(store, run, config, *, conditions=None, relation="fixed_initial", **extra):
    text = store.load_request(run).original_text
    return commit_candidate(store, run, candidate(store, run, kind="normalize", conditions=(
        defaults(config) if conditions is None else conditions), goals=[{
            "key": "energy", "port": "energy", "system_refs": ["water"], "text_basis": text,
            "geometry_relation": relation}], **extra), decision_id="text_normalize",
        basis=current_basis(store, run))


@pytest.mark.parametrize(("text", "name"), [
    ("优化水分子并给出电子能", "water"), ("Optimize methane and report energy.", "methane"),
    ("Initial geometry single-point electronic energy of H2O.", "water"),
    ("Initial geometry single-point electronic energy of CH4.", "methane"),
    ("Compute methane in water solvent.", "methane"),
    ("以水为溶剂研究甲烷。", "methane"),
    ("Can you optimize water?", "water"),
])
def test_supported_text_intake_freezes_only_identity_intent(tmp_path, monkeypatch, text, name):
    store, config = text_environment(tmp_path, permission={"model_execution": True,
        "scientific_execution": True, "external_identity_queries": True, "geometry_preparation": True,
        "artifact_writes": True, "allowed_tools": ["structure.resolve", "structure.prepare", "orca.sp"]})
    monkeypatch.setattr(doctor, "diagnose", lambda *_: pytest.fail("intake probed ORCA"))
    monkeypatch.setattr(structure, "_http_get", lambda *_: pytest.fail("intake queried PubChem"))
    monkeypatch.setattr(structure, "_generate", lambda *_: pytest.fail("intake generated geometry"))
    run = initialize_text(store, config, text)
    request = store.load_request(run)
    assert request.original_text == text and request.normalization_status == "pending"
    assert len(request.systems) == 1 and request.systems[0].id == name
    assert request.systems[0].geometry_source == "prepare"
    assert request.systems[0].identity["canonical_names"] == [name]
    assert request.geometry_artifact_id is request.systems[0].geometry_artifact_id is None
    assert request.charge is request.multiplicity is request.method is request.basis is None
    assert request.conditions_source == {} and request.semantic_defaults == config.text.defaults
    assert run.permission.model_dump() == config.text.permission.model_dump()
    assert run.science_baseline_policy == "first_science_plan"
    assert not run.attempts and not run.calls and not run.model_records
    assert run.usage.identity_queries == run.usage.structure_preparations == run.usage.orca_starts_actual == 0
    assert store.read_control(run.id)["messages"][0]["text"] == text


@pytest.mark.parametrize("text", ["优化乙醇", "Compute ammonia energy", "Calculate carbon dioxide.",
    "Calculate water and methane.", "计算那个分子的电子能", "Calculate molecule X.",
    "The molecule is not water. Compute its energy.", "Is that molecule water? Compute its energy.",
    "Water is not the target. Compute the target energy.", "The target isn't water. Compute its energy.",
    "分子身份未知，也许是水，先问清楚。", "那个分子是水吗？计算它的电子能。"])
def test_multiple_unknown_and_unsupported_text_never_selects_a_supported_substitute(tmp_path, text):
    store, config = text_environment(tmp_path)
    run = initialize_text(store, config, text)
    request = store.load_request(run)
    assert not request.systems and request.original_text == text
    assert request.normalization_status == "pending"
    assert request.goals[0].unresolved == ["missing:goal_definition"]
    assert not run.calls and not run.attempts


@pytest.mark.parametrize("profile", [{"enabled": False}, {"permission": {"model_execution": False}}])
def test_text_requires_both_explicit_profile_and_model_permission(tmp_path, profile):
    store, config = text_environment(tmp_path, **profile)
    with pytest.raises(StoreError):
        initialize_text(store, config, "Optimize water")
    assert not list((store.root / "runs").glob("*/run.json"))


@pytest.mark.parametrize("text", ["", " \n\t", "x" * 8193, None, b"water"])
def test_text_bounds_reject_before_run(tmp_path, text):
    store, config = text_environment(tmp_path)
    with pytest.raises(StoreError, match="1 to 8192"):
        initialize_text(store, config, text)
    assert not list((store.root / "runs").glob("*/run.json"))


def test_example_profile_is_explicit_finite_and_keeps_geometry_relation_undecided():
    config = load_config(Path(__file__).resolve().parents[2] / "config.text.example.toml")
    assert config.text.enabled and config.text.permission.model_execution
    assert config.text.permission.external_identity_queries and config.text.permission.geometry_preparation
    assert config.text.budget.identity_queries == config.text.budget.structure_preparations == 1
    assert config.text.budget.orca_starts == 1 and config.text.budget.extra_orca_starts == 0
    assert config.text.permission.max_cores == 4 and config.text.permission.max_memory_mb == 1024
    assert config.text.defaults == {"method": "HF", "basis": "STO-3G", "charge": 0,
        "multiplicity": 1, "electronic_state": "RHF", "environment": "gas_phase"}
    assert not Config().text.enabled


@pytest.mark.parametrize("source", ["text", "stdin", "bundle"])
def test_cli_selects_one_input_and_preserves_exact_text(tmp_path, monkeypatch, capsys, source):
    store, config = text_environment(tmp_path, enabled=source != "bundle")
    text = "Calculate initial geometry single-point energy of water.\nKeep this original text."
    monkeypatch.setattr(cli, "load_config", lambda _: config)
    monkeypatch.setattr("orca_agent.store.Store", lambda _: store)
    seen = []
    def execute(store, config, run_id, **kwargs):
        run = store.load_run(run_id)
        seen.append((store.load_request(run).original_text, kwargs))
        run.state = "paused"
        return run
    monkeypatch.setattr(runner, "execute", execute)
    if source == "bundle":
        path = tmp_path / "bundle.json"
        path.write_text(json.dumps({"text": text, "allowed_tools": []}), encoding="utf-8")
        args = ["ask", str(path)]
    elif source == "stdin":
        class BoundedInput(io.StringIO):
            def read(self, size=-1):
                assert size == 8193
                return super().read(size)
        monkeypatch.setattr(cli.sys, "stdin", BoundedInput(text))
        args = ["ask", "--stdin"]
    else:
        args = ["ask", "--text", text]
    assert cli.main(args) == 2
    assert seen == [(text, {"resume": False})]
    assert "run_id" in capsys.readouterr().out


@pytest.mark.parametrize("arguments", [[], ["bundle.json", "--text", "water"],
                                       ["bundle.json", "--stdin"], ["--text", ""]])
def test_cli_invalid_or_conflicting_inputs_do_not_start_coordinator(tmp_path, monkeypatch, arguments):
    store, config = text_environment(tmp_path)
    monkeypatch.setattr(cli, "load_config", lambda _: config)
    monkeypatch.setattr("orca_agent.store.Store", lambda _: store)
    monkeypatch.setattr(runner, "execute", lambda *_args, **_kw: pytest.fail("unexpected execution"))
    assert cli.main(["ask", *arguments]) == 2
    assert not list((store.root / "runs").glob("*/run.json"))


def test_cli_text_and_stdin_are_argparse_mutually_exclusive():
    with pytest.raises(SystemExit) as error:
        cli.main(["ask", "--text", "water", "--stdin"])
    assert error.value.code == 2


def test_cli_oversized_stdin_does_not_start_coordinator(tmp_path, monkeypatch):
    store, config = text_environment(tmp_path)
    monkeypatch.setattr(cli, "load_config", lambda _: config)
    monkeypatch.setattr("orca_agent.store.Store", lambda _: store)
    monkeypatch.setattr(cli.sys, "stdin", io.StringIO("x" * 100000))
    monkeypatch.setattr(runner, "execute", lambda *_args, **_kw: pytest.fail("unexpected execution"))
    assert cli.main(["ask", "--stdin"]) == 2
    assert not list((store.root / "runs").glob("*/run.json"))


def test_text_defaults_are_reported_as_defaults_after_production_normalization(tmp_path):
    store, config = text_environment(tmp_path)
    run = initialize_text(store, config, "Calculate initial geometry single-point electronic energy of water.")
    run = normalize(store, run, config)
    request = store.load_request(run)
    assert request.charge == 0 and request.multiplicity == 1
    assert all(request.conditions_source[key] == "default" for key in config.text.defaults)
    assert all(request.condition_evidence["request." + key]["default_rule"] == "local-hf-1"
               for key in config.text.defaults)
    assert not request.goals[0].unresolved and not run.calls and not run.attempts


@pytest.mark.parametrize("permission_change", [{"allowed_tools": []}, {"external_identity_queries": False},
                                              {"artifact_writes": False}])
def test_text_does_not_supply_missing_tool_or_effect_authorization(tmp_path, permission_change):
    permission = {"model_execution": True, "allowed_tools": ["structure.resolve"],
                  "external_identity_queries": True, "artifact_writes": True, **permission_change}
    store, config = text_environment(tmp_path, permission=permission)
    run = initialize_text(store, config, "Calculate initial geometry single-point energy of water.")
    run = normalize(store, run, config)
    request = store.load_request(run)
    step = Step(id="resolve", logical_id="resolve", tool="structure.resolve", system_id="water",
                parameters={"system_id": "water"})
    plan = Plan(request_id=request.id, request_version=request.version, steps=[step],
                goal_map={request.goals[0].id: OutputBinding(port="energy", gap="input acquisition pending")})
    with pytest.raises((StoreError, ValueError)):
        store.commit_revision(run, plan, decision_id="forbidden", basis=current_basis(store, run))
    saved = store.load_run(run.id)
    assert not saved.calls and not saved.attempts and saved.usage.identity_queries == 0


@pytest.mark.parametrize("explicit", ["The charge of water is 1.", "The charge of water is unknown."])
def test_allowed_defaults_cannot_replace_explicit_or_unknown_user_conditions(tmp_path, explicit):
    store, config = text_environment(tmp_path)
    run = initialize_text(store, config, "Calculate initial geometry single-point energy of water. " + explicit)
    with pytest.raises((StoreError, ValueError)):
        normalize(store, run, config)
    assert store.load_run(run.id).request_version == 1


def test_explicit_user_condition_keeps_explicit_provenance_and_supported_scope_gap(tmp_path):
    store, config = text_environment(tmp_path)
    run = initialize_text(store, config, "Calculate initial geometry single-point energy of water. Charge 1.")
    conditions = defaults(config)
    conditions["charge"] = {"value": 1, "source": "explicit", "text_basis": "Charge 1"}
    run = normalize(store, run, config, conditions=conditions,
                    notices=["Charge 1 is outside the current neutral-water supported scope."])
    request = store.load_request(run)
    assert request.charge == 1 and request.conditions_source["charge"] == "explicit"
    assert any("charge" in gap for gap in request.goals[0].unresolved)


@pytest.mark.parametrize("text", ["Calculate water's energy.", "算水的能量"])
def test_text_energy_without_geometry_relation_waits_without_effects(tmp_path, text):
    store, config = text_environment(tmp_path)
    run = initialize_text(store, config, text)
    proposal = {"action": "normalize_request", "parameters": candidate(store, run, kind="normalize",
        conditions=defaults(config), goals=[{"key": "energy", "port": "energy", "system_refs": ["water"],
            "text_basis": text}], questions=["请确认初始几何单点能还是优化后能量？"])}
    stopped = agent.execute(store, config, run.id, transport=ScriptedTransport(proposal))
    assert stopped.state == "waiting_user"
    assert not stopped.calls and not stopped.attempts
    assert stopped.usage.identity_queries == stopped.usage.structure_preparations == 0
    assert stopped.usage.orca_starts_actual == 0


def identity_answer(tmp_path, text="The molecule is water."):
    store, config = text_environment(tmp_path)
    run = initialize_text(store, config, "Calculate the initial geometry energy of that molecule.")
    request = store.load_request(run)
    message = {"id": "answer", "text": text, "request_version": request.version}
    values = {"goal_bindings": {"energy": ["water"]}}
    return store, run, request, message, values


@pytest.mark.parametrize("text", ["The molecule is water.", "水", "The target is water. Do not execute.",
                                  "The target is water; charge unknown."])
def test_confirmed_later_text_identity_registers_a_prepare_intent_only(tmp_path, text):
    _, run, request, message, values = identity_answer(tmp_path, text)
    before = request.model_dump_json(), run.model_dump_json()
    updated = bind_text_identity(request, run, [message], values)
    assert len(updated.systems) == 1 and updated.systems[0].id == "water"
    assert updated.systems[0].geometry_source == "prepare"
    assert updated.systems[0].identity["text_evidence"] == {"message_id": "answer", "text_basis": text}
    assert (request.model_dump_json(), run.model_dump_json()) == before


@pytest.mark.parametrize("text", ["Is it water?", "Maybe water", "The molecule identity is unknown; water.",
    "不是水", "Do not select water.", "It is not actually water.", "不要选择水", "water or methane", "ethanol", "水作为溶剂"])
def test_later_uncertain_negative_multiple_or_solvent_identity_is_not_registered(tmp_path, text):
    _, run, request, message, values = identity_answer(tmp_path, text)
    assert bind_text_identity(request, run, [message], values) is request


def test_later_answer_cannot_relabel_a_named_goal_or_enable_old_entry(tmp_path):
    _, run, request, message, values = identity_answer(tmp_path)
    request.goals = [Goal(id="energy", port="energy", minimum_check_version="orca-hf-2",
                          original_text="Methane energy", identity={"canonical_names": ["methane"]})]
    assert bind_text_identity(request, run, [message], values) is request
    request.goals[0].original_text = "energy"
    request.goals[0].identity = {}
    run.science_baseline_policy = "legacy"
    assert bind_text_identity(request, run, [message], values) is request


@pytest.mark.parametrize(("answer", "relation"), [
    ("Use the initial geometry single-point energy.", "fixed_initial"),
    ("Report the optimized electronic energy.", "optimized"),
    ("采用初始几何单点能。", "fixed_initial"), ("给出优化后的电子能。", "optimized"),
])
def test_geometry_relation_answer_updates_same_goal_with_authenticated_source(tmp_path, answer, relation):
    store, config = text_environment(tmp_path)
    text = "Calculate water's energy."
    run = initialize_text(store, config, text)
    run = normalize(store, run, config, relation=None,
                    questions=["请确认初始几何单点能还是优化后能量？"])
    before = store.load_request(run)
    assert before.goals[0].unresolved == ["ambiguous_geometry_relation"]
    message_id = store.enqueue_message(run.id, answer)
    run = commit_candidate(store, run, candidate(store, run, kind="amend",
        resolves=["ambiguous_geometry_relation"]), decision_id="relation_answer", basis=current_basis(store, run))
    request = store.load_request(run)
    assert request.goals[0].id == before.goals[0].id
    assert request.goals[0].original_text == text and not request.goals[0].unresolved
    assert request.goals[0].conditions["geometry_relation"] == relation
    assert request.goals[0].text_evidence["geometry_relation"]["message_ids"] == [message_id]
    assert not run.calls and not run.attempts


@pytest.mark.parametrize(("answer", "name"), [("The molecule is water.", "water"), ("甲烷", "methane")])
def test_later_named_answer_registers_prepare_system_in_production_commit(tmp_path, answer, name):
    store, config = text_environment(tmp_path)
    text = "Calculate the initial geometry single-point energy of that molecule."
    run = initialize_text(store, config, text)
    run = commit_candidate(store, run, candidate(store, run, kind="normalize", conditions=defaults(config),
        goals=[{"key": "energy", "port": "energy", "text_basis": text,
                "geometry_relation": "fixed_initial", "unresolved": ["ambiguous_system"]}],
        questions=["Which molecule is the target?"]), decision_id="unknown_identity", basis=current_basis(store, run))
    before = store.load_request(run)
    goal_id = before.goals[0].id
    message_id = store.enqueue_message(run.id, answer)
    run = commit_candidate(store, run, candidate(store, run, kind="amend",
        goal_bindings={goal_id: [name]}, resolves=["ambiguous_system"]),
        decision_id="named_answer", basis=current_basis(store, run))
    request = store.load_request(run)
    assert len(request.systems) == 1 and request.systems[0].id == name
    assert request.systems[0].geometry_source == "prepare"
    assert request.systems[0].identity["text_evidence"] == {"message_id": message_id, "text_basis": answer}
    assert request.goals[0].id == goal_id and request.goals[0].system_ids == [name]
    assert request.goals[0].identity["canonical_names"] == [name]
    assert not request.unresolved and not request.goals[0].unresolved
    assert not run.calls and not run.attempts
    assert run.permission.model_dump() == config.text.permission.model_dump()
