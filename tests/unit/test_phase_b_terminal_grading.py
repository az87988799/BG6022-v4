"""New evaluation gates use original replies, not a repaired program report."""

import copy
import json
from dataclasses import replace

import pytest

from orca_agent import agent
from orca_agent.config import Config
from tests.helpers import phase_b_model_cases as cases
from tests.helpers.phase_b_grading import classify_grade, terminal_delivery_evidence
from tests.unit.test_agent import ScriptedTransport
from tests.unit.test_terminal_recovery import make_partial


def delivered(tmp_path):
    store, run, artifact = make_partial(tmp_path)
    ended = agent.execute(store, Config(), run.id,
        transport=ScriptedTransport({"action": "stop", "reason": "Synthetic audit prose; required b is missing."}))
    assert ended.terminal_deliveries and ended.state == "failed"
    return store, ended, artifact


def test_original_stop_snapshot_receipt_and_current_report_match(tmp_path):
    store, run, _ = delivered(tmp_path)
    result = terminal_delivery_evidence(store, run)
    assert result["passed"], result
    assert result["accepted_action"] == "stop"
    assert result["snapshot_fingerprint"] == run.terminal_deliveries[-1].snapshot_fingerprint
    # A partial scientific result can have a valid explanation contract. These
    # synthetic receipts still do not establish real-model evaluation evidence.
    assert run.goal_status["b"] != "satisfied"


@pytest.mark.parametrize("mutation", ["receipt", "decision", "reason", "response", "snapshot",
                                     "source", "report", "publication", "reopened", "permission"])
def test_mutated_binding_cannot_be_repaired_by_correct_program_rendering(tmp_path, mutation):
    store, run, artifact = delivered(tmp_path)
    receipt = run.terminal_deliveries[-1]
    if mutation == "receipt":
        receipt.explanation["goal_explanations"].pop()
    elif mutation == "decision":
        run.decisions[-1]["parameters"]["delivery"]["goal_explanations"].pop()
    elif mutation == "reason":
        run.decisions[-1]["reason"] = "Program substituted a correct explanation."
    elif mutation == "response":
        path = store.path(f"runs/{run.id}/model/{receipt.decision_id}.response.json")
        data = json.loads(path.read_text())
        data["proposal"]["reason"] = "Changed model evidence"
        path.write_text(json.dumps(data))
    elif mutation == "snapshot":
        receipt.snapshot["goals"][0]["original_text"] = "Different goal"
    elif mutation == "source":
        store.artifact_path(artifact.id).write_text('{"a":999,"b":2}')
    elif mutation == "report":
        store.path(receipt.report_path).write_text("Changed report")
    elif mutation == "publication":
        receipt.report_status = "failed"
    elif mutation == "reopened":
        run.reopened_terminal_ids.append(receipt.decision_id)
    else:
        run.permission.scientific_execution = True
    assert terminal_delivery_evidence(store, run)["passed"] is False


def test_report_contract_cannot_override_failed_independent_prose_review(tmp_path):
    store, run, _ = delivered(tmp_path)
    contract = terminal_delivery_evidence(store, run)
    assert contract["passed"]
    grade = {"status": "passed", "real_model_evidence_present": True,
        "safety_invariants_passed": True, "assertions": [{"status": "passed"}],
        "required_final_response_accepted": {"required": True, **contract},
        "proposal_review": {"status": "failed", "all_proposal_facts_passed": False,
                            "semantic_review_passed": False}}
    assert classify_grade(grade) == "failed"


def test_current_evaluation_needs_receipt_but_legacy_gate_remains_readable(tmp_path):
    from orca_agent.model_usage import current_basis
    from orca_agent.store import Store
    from orca_agent.tools.dispatch import execute_call

    store = Store(tmp_path / "data", environment_root=tmp_path / "environment")
    run, metadata = cases.create_request(store, "V-07/discover-and-read", 1)
    request = store.load_request(run)
    query = next(g.conditions["query"] for g in request.goals if g.port == "value_observation")
    execute_call(store, run, "evidence.value", query)
    run.decisions.append({"id": "synthetic_legacy", "action": "stop", "parameters": {},
                          "reason": "Synthetic old receipt-free stop", "basis": current_basis(store, run)})
    run.state = "failed"
    store.save_run(run)
    assert not cases.evaluate_response(store, run, metadata)["required_final_response_accepted"]["passed"]
    legacy = copy.deepcopy(metadata)
    legacy.pop("terminal_contract_version")
    assert cases.evaluate_response(store, run, legacy)["required_final_response_accepted"]["passed"]


def test_new_sampling_variants_are_separate_from_old_fixed_allocation(tmp_path):
    from orca_agent.model_usage import current_basis
    from orca_agent.semantic import VERSION, commit_candidate
    from orca_agent.store import Store, sha256_file

    old = sha256_file(cases.CASES)
    variants = cases.sampling_intent_variant_ids()
    assert len(variants) == 6
    real = cases.sampling_intent_variant_ids(real_model_only=True)
    assert len(real) == 3 and all(v in cases.evaluation_variant_ids() for v in real)
    assert len(cases.fixed_variant_ids()) == 25
    store = Store(tmp_path / "data", environment_root=tmp_path / "environment")
    for variant in real:
        run, metadata = cases.create_request(store, variant, 1)
        request = store.load_request(run)
        assert request.goals[0].port == "sampling"  # Model must interpret the pending replacement.
        assert run.permission.allowed_tools == ["analysis.finite_sampling", "analysis.sampling_check"]
        message = store.read_control(run.id)["messages"][-1]
        assert message["id"] == metadata["semantic_update"]["message_id"]
        assert message["text"] == cases.variant_spec(variant)["input"]["text"]
        assert message["id"] not in run.processed_messages
        assert not run.permission.scientific_execution and not run.attempts and not run.model_records
        assert run.budget.orca_starts == 0 and run.budget.model_calls == 4 and run.budget.model_tokens == 48000
        assert metadata["terminal_contract_version"] == "terminal-delivery-1"
        # A clean clone without the historical Store must retain explicit gaps.
        assert metadata["fixture_gaps"]
        selected_port = "sampling_check" if cases.variant_spec(variant)["input"]["intent"] == "check" else "sampling"
        parameters = {"schema_version": VERSION, "message_ids": [message["id"]], "kind": "replace_goals",
            "text_basis": message["text"], "replaces": [request.goals[0].id], "goals": [{"key": "selected",
                "port": selected_port, "text_basis": message["text"], "analysis_goal_ref": request.goals[0].id}]}
        changed = commit_candidate(store, run, parameters, decision_id="synthetic_semantic_update",
                                   basis=current_basis(store, run))
        assert store.load_request(changed).goals[0].port == selected_port
    for variant in set(variants) - set(real):
        with pytest.raises(ValueError, match="outside"):
            cases.create_request(store, variant, 1)
    assert sha256_file(cases.CASES) == old


@pytest.mark.parametrize("variant", cases.sampling_intent_variant_ids(real_model_only=True))
@pytest.mark.parametrize("correction", [None, "context", "budget"],
                         ids=["direct", "normalization-correction", "correction-budget"])
def test_sampling_model_update_then_plan_analysis_and_contract(tmp_path, monkeypatch, variant, correction):
    """Synthetic model replay; archived files parsed offline, never live ORCA."""
    from orca_agent.llm import ModelUsage
    from orca_agent.semantic import VERSION
    from orca_agent.store import Store, sha256_file
    from orca_agent.tools.analysis import bind_energy
    from tests.unit.test_dispatch import archived_energy

    def reference(store, reference_id, metadata):
        fixture = cases.PROJECT / "tests/fixtures/phase_b/independent" / reference_id
        origin, result = archived_energy(store, fixture)
        evidence = bind_energy(store, origin.id, result.id, expected_attempt_id=result.attempt_id)
        metadata["references"].append({"result_id": result.id, "run_id": origin.id,
            "attempt_id": result.attempt_id, "raw_stdout": {"path": str(fixture / "stdout.out"),
                                                           "sha256": sha256_file(fixture / "stdout.out")}})
        return {"binding": {"run_id": origin.id, "result_id": result.id, "attempt_id": result.attempt_id,
                            "port": "energy", "rule_version": "orca-hf-2"},
                "geometry_artifact_id": evidence.geometry_artifact_id,
                "conditions": evidence.conditions.model_dump(), "geometry_sha256": evidence.geometry_sha256}

    monkeypatch.setattr(cases, "_reference", reference)
    if correction == "budget":
        # Freeze a smaller synthetic allocation at creation, never mutate the
        # durable Run budget. The real formal fixture remains unchanged at 48k.
        original_spec = cases.variant_spec
        def smaller_allocation(identifier):
            spec = copy.deepcopy(original_spec(identifier))
            spec["budget"]["model_tokens_total"] = 20000
            return spec
        monkeypatch.setattr(cases, "variant_spec", smaller_allocation)
    store = Store(tmp_path / "data", environment_root=tmp_path / "environment")
    run, metadata = cases.create_request(store, variant, 1)
    original = store.load_request(run).model_dump(mode="json")
    text = metadata["semantic_update"]["text"]
    port = "sampling_check" if cases.variant_spec(variant)["input"]["intent"] == "check" else "sampling"
    tool = "analysis.sampling_check" if port == "sampling_check" else "analysis.finite_sampling"

    def normalize(data):
        assert store.load_request(store.load_run(run.id)).goals[0].port == "sampling"
        return {"action": "normalize_request", "parameters": {"schema_version": VERSION,
            "message_ids": [metadata["semantic_update"]["message_id"]], "kind": "replace_goals",
            "text_basis": text, "replaces": [original["goals"][0]["id"]], "goals": [{
                "key": "selected", "port": port, "text_basis": text,
                "message_id": metadata["semantic_update"]["message_id"],
                "analysis_goal_ref": original["goals"][0]["id"]}]}}

    def plan(data):
        current = data["AUTHORITY"]["request"]
        goal = current["goals"][0]
        assert goal["port"] == port
        return {"action": "initial_plan", "parameters": {"steps": [{"key": "assess", "tool": tool,
            "parameters": {"goal_id": goal["id"]}, "inputs": {name: item["binding"]
                for name, item in current["conditions"]["available_evidence"].items()}}],
            "goal_map": {goal["id"]: {"step_key": "assess", "port": port}}}}

    class OfflineBatch:
        def reserve_model(self, *args):
            pass

        def settle_model(self, *args):
            pass

    class FullBoundTransport(ScriptedTransport):
        """Charge actual prepared upper bounds, never the usual synthetic 75."""
        bounds = []
        attempted_bounds = []

        def send(self, prepared, *, reserve, settle):
            row = {"input": prepared.input_token_bound, "output": prepared.output_token_bound,
                   "reserved": prepared.reserved_tokens}
            self.attempted_bounds.append(row)
            usage = ModelUsage(row["input"], row["output"], row["reserved"])

            def checked_reserve(value):
                ticket = reserve(value)
                self.bounds.append(row)
                return ticket

            def charge(ticket, reply):
                settle(ticket, replace(reply, usage=usage))

            return replace(super().send(prepared, reserve=checked_reserve, settle=charge), usage=usage)

    scripts = [normalize, plan, {"action": "stop", "reason": "Synthetic finite predicate report."}]
    if correction:
        # The measured most expensive stage is normalization. The rejected
        # response and its actual feedback are durably retained; no fake
        # token settlement is used to fit a fourth transmission.
        def incomplete_normalization(data):
            proposal = normalize(data)
            del proposal["parameters"]["schema_version"]
            return proposal
        scripts.insert(0, incomplete_normalization)
    transport = FullBoundTransport(*scripts)
    ended = agent.execute(store, Config(), run.id, batch=OfflineBatch(),
        transport=transport)
    (tmp_path / "capacity.json").write_text(json.dumps({"variant": variant, "correction": correction, "budget": run.budget.model_tokens,
        "requests": transport.bounds, "attempted_requests": transport.attempted_bounds, "sum_reservations": sum(row["reserved"] for row in transport.bounds),
        "remaining": run.budget.model_tokens - sum(row["reserved"] for row in transport.bounds),
        "diagnostics": ended.diagnostics}, indent=2), encoding="utf-8")
    assert ended.usage.model_tokens_used == sum(row["reserved"] for row in transport.bounds)
    assert ended.usage.model_tokens_used <= run.budget.model_tokens
    assert ended.usage.model_calls <= run.budget.model_calls
    assert ended.usage.model_tokens_unknown == 0
    assert all(row["input"] <= 12000 and row["output"] == 2000 for row in transport.bounds)
    assert sum(decision.get("action") == "rejected" for decision in ended.decisions) == int(correction == "context")
    assert ended.usage.orca_starts_actual == 0 and not ended.attempts
    assert ended.usage.identity_queries == ended.usage.structure_preparations == 0
    if correction == "budget" or correction == "context" and ended.usage.model_calls < 4:
        # Corrections are bounded by both available reservations and context.
        # Rejected evidence remains; a deterministic report must still exist.
        from orca_agent.report import build_report
        if correction == "budget":
            assert ended.state == "budget_exhausted"
            assert ended.usage.model_calls == 0 and len(transport.scripts) == 4
            assert transport.attempted_bounds and not transport.bounds
            assert any(d["category"] == "BudgetExceeded" for d in ended.diagnostics)
        else:
            assert ended.state in {"failed", "budget_exhausted"}, (ended.diagnostics, transport.bounds)
            assert 1 <= ended.usage.model_calls <= 3 and transport.scripts
            assert any(d["category"] in {"ContextLimitError", "BudgetExceeded"} for d in ended.diagnostics)
        assert not ended.terminal_deliveries
        report = build_report(store, ended)
        assert report["report_artifact"]["status"] == "rendered"
        assert report["delivery"]["facts"]
        return
    assert ended.state == ("failed" if port == "sampling" else "completed"), (ended.diagnostics, transport.bounds)
    assert ended.usage.model_calls == 3 + int(bool(correction)) and ended.usage.analysis_executions == 1
    current = store.load_request(ended)
    assert current.goals[0].port == port
    assert current.goals[0].conditions == original["goals"][0]["conditions"]
    assert store.load_request_revision(ended, 1).model_dump(mode="json") == original
    assert terminal_delivery_evidence(store, ended)["passed"]
    assert metadata["semantic_update"]["message_id"] in ended.processed_messages
