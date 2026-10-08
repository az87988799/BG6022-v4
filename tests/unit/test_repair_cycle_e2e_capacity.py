"""Fixed repair-cycle text/profile capacity, entirely synthetic and offline.

The five scripted decisions are not real model understanding. Synthetic model
usage only allows the production loop to expose each request shape; the sum of
the conservative request bounds is reported separately, never claimed to prove
that a real trajectory fits the 48k Run budget. No process or HTTP is launched.
"""

import ast
import inspect
import json
import shutil
from copy import deepcopy

import pytest
from test_agent import ScriptedTransport
from test_optimization_final_stage import _case_text
from test_orca import synthetic_output
from test_semantic_control import candidate
from test_structure_tools import mock_generator, response

from orca_agent import agent, context, doctor
from orca_agent.config import Config
from orca_agent.natural import initialize_text
from orca_agent.orca.adapter import prepare_input
from orca_agent.report import build_report
from orca_agent.store import Store
from orca_agent.tools import electronic, geometry, structure
from orca_agent.tools.calculation import collect_result, revalidate
from tests.helpers import phase_b_repair_cycle as cycle
from tests.helpers import phase_b_repair_cycle_execution as execution
from tests.unit.test_decision_purpose_capacity import expand_schema


def _fixed_text(system):
    # Reuse the exact future operator text without entering any live helper or
    # duplicating a near-equivalent wording in this capacity fixture.
    tree = ast.parse(inspect.getsource(execution.e2e_slot))
    assignment, = [node for node in ast.walk(tree) if isinstance(node, ast.Assign)
                   and any(isinstance(target, ast.Name) and target.id == "text" for target in node.targets)]
    assert isinstance(assignment.value, ast.IfExp)
    return ast.literal_eval(assignment.value.body if system == "water" else assignment.value.orelse)


def _parts(messages):
    wire = json.loads(messages[1]["content"])
    def size(value):
        return len(json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
    return {"system_bytes": len(messages[0]["content"].encode("utf-8")),
            "user_bytes": len(messages[1]["content"].encode("utf-8")),
            "wire_top_level_bytes": {key: size(value) for key, value in wire.items()},
            "purpose": wire["AUTHORITY"]["decision_purpose"]["kind"]}


@pytest.mark.parametrize("system", ["water", "methane"])
@pytest.mark.parametrize("path_shape", ["temporary", "development", "formal"])
def test_fixed_e2e_profile_every_normal_stage_fits_without_live_execution(tmp_path, monkeypatch, system, path_shape):
    run_fixed_e2e(tmp_path, monkeypatch, system, path_shape)


def run_fixed_e2e(tmp_path, monkeypatch, system, path_shape, *, goal_names=None, correction=False):
    """Reuse the fixed scope with explicit presentation variations only."""
    config = execution._e2e_profile(Config(), category="development")
    # Explicit synthetic executable identities satisfy the ordinary environment
    # receipt contract; the scientific Tool boundary below never starts them.
    config.orca_path, config.mpi_path = tmp_path / "orca-环境.synthetic", tmp_path / "mpi-环境.synthetic"
    config.orca_path.write_bytes(b"synthetic identity; not executable")
    config.mpi_path.write_bytes(b"synthetic identity; not executable")
    monkeypatch.setattr(doctor, "diagnose", lambda _: {
        "orca": {"compatible": True, "version": "6.1.1", "path": str(config.orca_path)},
        "mpi": {"path": str(config.mpi_path)}, "evidence_kind": "offline_synthetic"})
    actual_store_root = None
    store_root = tmp_path / "data"
    if path_shape != "temporary":
        number = 3 if path_shape == "development" else 2
        actual_store_root = execution._root(cycle.candidate_label(path_shape, number)) / "agent"
        # Match the real deepest candidate's raw UTF-8 length and separator
        # count while every file remains beneath pytest's temporary directory.
        # If another platform's tmp root is already longer, test that stricter
        # shape and record both sizes instead of shortening or using real data.
        segments = ["p"] * max(1, len(actual_store_root.parts) - len(tmp_path.parts) - 1) + ["agent"]
        simulated = tmp_path.joinpath(*segments)
        padding = max(0, len(str(actual_store_root).encode("utf-8")) - len(str(simulated).encode("utf-8")))
        segments[0] += "p" * padding
        store_root = tmp_path.joinpath(*segments)
        assert store_root.is_relative_to(tmp_path)
        assert len(str(store_root).encode("utf-8")) >= len(str(actual_store_root).encode("utf-8"))
        assert len(store_root.parts) >= len(actual_store_root.parts)
    store = Store(store_root, environment_root=tmp_path / "environment")
    text = _fixed_text(system)
    run = initialize_text(store, config, text)
    assert run.budget.model_calls == 8 and run.budget.model_tokens == 48000
    assert run.budget.input_tokens == 12000 and run.budget.orca_starts == 1
    assert set(run.permission.allowed_tools) == {"structure.resolve", "structure.prepare", "orca.opt", "orca.sp"}
    assert store.load_request(run).geometry_artifact_id is None and not run.result_ids
    tool = "orca.opt" if system == "water" else "orca.sp"
    ports = ["optimized_geometry", "energy"] if system == "water" else ["energy"]
    goal_names = goal_names or {port: port for port in ports}
    assert set(goal_names) == set(ports)
    relation = "optimized" if system == "water" else "fixed_initial"
    normalize = {"action": "normalize_request", "parameters": candidate(store, run, kind="normalize",
        goals=[{"key": goal_names[port], "port": port, "system_refs": [system], "text_basis": text,
                "geometry_relation": relation} for port in ports],
        conditions={key: {"value": value, "source": "explicit", "text_basis": text}
                    for key, value in config.text.defaults.items()})}
    plan = {"action": "initial_plan", "parameters": {"steps": [
        {"key": "identity", "tool": "structure.resolve", "system_id": system, "parameters": {"system_id": system}},
        {"key": "prepare", "tool": "structure.prepare", "system_id": system,
         "parameters": {"system_id": system, "charge": 0, "multiplicity": 1},
         "inputs": {"identity": {"producer_key": "identity", "port": "resolved_identity"}}},
        {"key": "science", "tool": tool, "system_id": system, "parameters": {},
         "geometry": {"producer_key": "prepare", "port": "prepared_geometry"}}],
        "goal_map": {"goal_" + goal_names[port]: {"step_key": "science", "port": port} for port in ports}}}

    def next_tool(name):
        def proposal(data):
            current = store.load_run(run.id)
            step = next(item for item in store.load_plan(current).steps if item.tool == name)
            return {"action": "call_tool", "parameters": {"step_id": step.id}}
        return proposal

    monkeypatch.setattr(structure, "_paced_query", lambda *_, **__: (200, response(system), True))
    generated = mock_generator(monkeypatch, system)
    scientific_calls = []

    def synthetic_science(store, run, step, attempt, config, fault=None):
        # The UTF-8 environment paths must survive every production read even
        # when this test's checkout/temporary directory happens to be ASCII.
        revalidate(store, run, step, config)
        scientific_calls.append(step.id)
        work = store.path(attempt.directory)
        prepare_input(work, store.artifact_path(attempt.geometry_artifact_id), step.parameters, step.tool)
        stdout = (_case_text(work / "geometry.xyz", "valid_final_evaluation")
                  if step.tool == "orca.opt" else synthetic_output(work / "geometry.xyz"))
        (work / "stdout.out").write_text(stdout, encoding="utf-8")
        shutil.copyfile(work / "geometry.xyz", work / "job.xyz")
        outcome = {"state": "completed", "reason": "synthetic capacity fixture; no process"}
        return collect_result(store, run, step, attempt, outcome), outcome

    monkeypatch.setattr(electronic, "execute", synthetic_science)
    monkeypatch.setattr(geometry, "execute", synthetic_science)
    attempts = []
    prepare = context.prepare_request
    source_schemas = []
    source_schema_by_hash = {}
    schema_columns = context._schema_columns
    def capture_schema(schema):
        source_schemas.append(deepcopy(schema))
        return schema_columns(schema)
    monkeypatch.setattr(context, "_schema_columns", capture_schema)
    build_context = agent.build_context
    def capture_context(*args, **kwargs):
        before = deepcopy((args, kwargs))
        try:
            return build_context(*args, **kwargs)
        finally:
            # Request/Plan/Run (including frozen Calls) and every selected
            # Result/snapshot remain exact while display digests are omitted.
            assert (args, kwargs) == before
    monkeypatch.setattr(agent, "build_context", capture_context)

    def capture(messages, **kwargs):
        item = _parts(messages)
        try:
            prepared = prepare(messages, **kwargs)
        except ValueError as exc:
            attempts.append({**item, "error": str(exc)})
            (tmp_path / "last-overflow.json").write_text(json.dumps({
                "kind": "offline_synthetic", "messages": messages, "parameters": kwargs,
                "error": str(exc)}, ensure_ascii=False, indent=2), encoding="utf-8")
            raise
        attempts.append({**item, "request_hash": prepared.request_hash,
                         "input_bound": prepared.input_token_bound, "output_bound": prepared.output_token_bound})
        if "SCHEMA_COLUMNS" in json.loads(messages[1]["content"]):
            source_schema_by_hash[prepared.request_hash] = deepcopy(source_schemas[-1])
        return prepared

    monkeypatch.setattr(context, "prepare_request", capture)
    stages = []

    class CapacityTransport(ScriptedTransport):
        def send(self, prepared, *, reserve, settle):
            current = store.load_run(run.id)
            if store.load_request(current).normalization_status == "pending":
                stage = "normalize"
            elif current.plan_id is None:
                stage = "initial_plan"
            else:
                stage = {1: "after_resolve", 2: "after_prepare", 3: "after_science"}[len(current.result_ids)]
            stages.append(stage)
            return super().send(prepared, reserve=reserve, settle=settle)

    scripts = [normalize, plan]
    if correction:
        def rejected(data):
            return {**next_tool("structure.prepare")(data), "type": "json_object"}
        scripts.append(rejected)
    transport = CapacityTransport(*scripts, next_tool("structure.prepare"), next_tool(tool),
        {"action": "stop", "parameters": {"reason": "Offline synthetic capacity fixture; no real science claim."}})
    ended = agent.execute(store, config, run.id, transport=transport)
    sent = []
    for stage, record in zip(stages, ended.model_records, strict=True):
        item = next(item for item in attempts if item.get("request_hash") == record["request_hash"])
        sent.append({"stage": stage, **item})
    summary = {"kind": "offline_scripted_capacity_not_real_model_or_science", "system": system,
        "text": text, "profile": config.text.model_dump(mode="json"), "sent": sent,
        "temporary_root_utf8_bytes": len(str(tmp_path).encode("utf-8")),
        "path_shape": path_shape, "actual_future_store_root": str(actual_store_root) if actual_store_root else None,
        "actual_store_utf8_bytes": len(str(actual_store_root).encode("utf-8")) if actual_store_root else None,
        "simulated_store_root": str(store_root), "simulated_store_utf8_bytes": len(str(store_root).encode("utf-8")),
        "actual_store_path_parts": len(actual_store_root.parts) if actual_store_root else None,
        "simulated_store_path_parts": len(store_root.parts),
        "synthetic_environment_paths": [str(config.orca_path), str(config.mpi_path)],
        "conservative_total_with_output_bounds": sum(item["input_bound"] + item["output_bound"] for item in sent),
        "actual_model_tokens": None, "synthetic_usage_is_not_budget_proof": True,
        "state": ended.state, "diagnostics": ended.diagnostics, "all_preparation_attempts": attempts}
    (tmp_path / "capacity.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    count = 6 if correction else 5
    assert len(sent) == count and len(transport.sent) == count and not transport.scripts, summary
    assert stages == ["normalize", "initial_plan", "after_resolve", *(["after_resolve"] if correction else []),
                      "after_prepare", "after_science"]
    assert all(item["input_bound"] <= 12000 and item["output_bound"] == 2000 for item in sent)
    assert len(generated) == len(scientific_calls) == 1
    assert ended.usage.identity_queries == ended.usage.structure_preparations == 1
    assert ended.usage.orca_starts_reserved == 1 and ended.usage.orca_starts_actual == 0
    report = build_report(store, ended)
    assert ended.state == "completed" and report["user_goal_complete"], summary
    assert set(ended.goal_status) == {"goal_" + goal_names[port] for port in ports}
    assert report["model_explanation"]["current_status"] == "passed"
    for data, record in zip(transport.sent, ended.model_records, strict=True):
        assert data["AUTHORITY"]["basis"]["permission_version"] == run.permission.version
        if "SCHEMA_COLUMNS" in data:
            schema = expand_schema(data["PROPOSAL_SCHEMA"])
            assert schema == source_schema_by_hash[record["request_hash"]]
            assert schema["additionalProperties"] is False and schema["minProperties"] == 8
    after_resolve = transport.sent[2]
    assert "immutable_frozen_details_sha256" not in after_resolve["AUTHORITY"]["plan"]
    assert "SCHEMA_SHA256" not in after_resolve
    frozen = ended.calls[0].frozen_step
    assert frozen == store.load_plan(ended).steps[0]
    assert after_resolve["AUTHORITY"]["plan"]["steps"][0] == {"id": frozen.id, "immutable_frozen": True}
    terminal = transport.sent[-1]["DATA"]["delivery"]
    assert {goal["port"] for goal in terminal["goals"]} == set(ports)
    # Capacity must retain scientific answers and conditions, not merely fit by
    # stripping facts while the scripted proposal selects their short refs.
    visible_facts = {fact["ref"]: fact for fact in terminal["facts"]}
    assert {fact["ref"] for fact in report["delivery"]["facts"]} == set(visible_facts)
    for fact in report["delivery"]["facts"]:
        if fact["kind"] in {"answer", "conditions", "goal_status"}:
            assert visible_facts[fact["ref"]]["value"] == context._safe(fact["value"])
    if correction:
        rejected_decisions = [decision for decision in ended.decisions if decision.get("action") == "rejected"]
        assert len(rejected_decisions) == 1
        assert transport.sent[3]["CONTROL"]["validation_error"]["requirement"] == [
            {"type": "extra_forbidden", "loc": ["type"]}]
        assert ended.usage.model_calls == 6 and ended.usage.model_tokens_used == 450
    return summary
