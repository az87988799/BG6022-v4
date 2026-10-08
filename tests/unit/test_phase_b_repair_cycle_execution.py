"""Offline operator boundaries. Fakes are explicit, never acceptance evidence."""

from types import SimpleNamespace

import pytest

from orca_agent.config import Config
from tests.helpers import phase_b_repair_cycle as cycle
from tests.helpers import phase_b_repair_cycle_execution as execution
from tests.unit.test_phase_b_repair_cycle import bound
from tests.unit.test_phase_b_repair_cycle import manager as manager


def test_development_manifest_preserves_every_separate_adopted_allocation():
    manifest = execution.development_manifest(1)
    actual = execution.manifest_budget(manifest)
    assert len(manifest["slots"]) == 52
    for name, amounts in actual.items():
        assert amounts == dict(zip(cycle.DIMENSIONS, cycle.ALLOCATIONS[name], strict=True))
    assert {key for key in actual if key.startswith("gates-")} == {"gates-1"}
    assert sum(row["declared"]["reference"] for key, row in manifest["slots"].items()
               if key.startswith("e2e-development/")) == 6


def test_formal_manifest_preserves_real_semantics_and_refuses_unapproved_overrun():
    from tests.helpers import phase_b_model_cases as cases
    manifest = execution.formal_manifest(1)
    assert len(manifest["coverage_bindings"]) == 228
    model_slots = [key for key in manifest["slots"] if key.startswith("formal-1/model-")]
    assert len(model_slots) == 3 * len(cases.evaluation_variant_ids()) == 120
    assert len([key for key in manifest["slots"] if key.startswith("formal-1/joint-")]) == 18
    assert len([key for key in manifest["slots"] if key.startswith("formal-1/offline-")]) == 87
    assert all(f"SC-01/{name}/{rep}" in manifest["coverage_bindings"]
               for name in ("check-positive", "check-negative", "acquisition-negative") for rep in (1, 2, 3))
    actual = execution.manifest_budget(manifest)["formal-1"]
    assert actual["http_requests"] > cycle.ALLOCATIONS["formal-1"][0]
    assert actual["tokens"] > cycle.ALLOCATIONS["formal-1"][1]
    with pytest.raises(execution.reference.ReferenceBlocked, match="exceeds a purpose allocation"):
        cycle._validate_manifest("formal", 1, manifest)


@pytest.mark.parametrize("entry,args", [("e2e_slot", ("water", 1)), ("joint_slot", ("sampling_left",))])
def test_new_execution_functions_have_no_default_side_effect(tmp_path, monkeypatch, entry, args):
    monkeypatch.setattr(execution.reference, "BATCH_ROOT", tmp_path / "no-data")
    with pytest.raises(execution.reference.ReferenceBlocked, match="explicit"):
        getattr(execution, entry)(cycle.candidate_label("development", 1), *args)
    assert not list(tmp_path.iterdir())


def passed_gates(manager, tmp_path):
    for index, variant in enumerate(cycle.MODEL_SLOTS):
        _, slot = bound(manager, variant)
        path = tmp_path / f"synthetic-gate-{index}.json"
        path.write_text('{"synthetic":true,"status":"passed"}')
        cycle.record_outcome(slot, status="passed", evidence=[{"path": str(path),
                                     "sha256": execution.sha256_file(path)}])


@pytest.mark.parametrize("system", ["water", "methane"])
def test_e2e_checkpoint_precedes_reference_and_resumes_same_run_deadline(manager, tmp_path, monkeypatch, system):
    from orca_agent import runner
    from tests.helpers import phase_b_bounded_package as package
    passed_gates(manager, tmp_path)
    _, _, label, _ = manager
    monkeypatch.setattr(execution, "_config", lambda candidate, store: Config(data_root=store.root))
    events = []
    seen = {}
    def fake_execute(store, config, run_id, *, resume=False, batch, fault):
        run = store.load_run(run_id)
        assert run.usage.orca_starts_actual == run.usage.orca_starts_reserved == 0
        assert not run.attempts
        if not resume:
            seen.update(run_id=run.id, deadline=run.deadline, original_request=store.load_request(run))
            assert not seen["original_request"].geometry_artifact_id
            assert not seen["original_request"].goals[0].port == "energy"
            assert config.text.enabled and run.budget.identity_queries == run.budget.structure_preparations == 1
            events.append("model-selected-science-before-reservation")
            fault("before_science_reservation")
            pytest.fail("checkpoint should leave the outer Run lock before reference")
        assert run.id == seen["run_id"] and run.deadline == seen["deadline"]
        assert events == ["model-selected-science-before-reservation", "independent-reference"]
        fault("before_science_reservation")
        events.append("resume-original-pending-decision")
        return run
    def fake_geometry(store, run):
        path = tmp_path / "synthetic-prepared.xyz"
        if not path.exists():
            path.write_text("3\nsynthetic operator fixture\nO 0 0 0\nH 0 1 0\nH 0 0 1\n")
        artifact = SimpleNamespace(id="synthetic-prepared", sha256=execution.sha256_file(path))
        step = SimpleNamespace(id="science", tool="orca.opt" if system == "water" else "orca.sp")
        return step, artifact, path
    def fake_reference(candidate, allocation, slot_id, geometry, *, job_type, config):
        assert events == ["model-selected-science-before-reservation"]
        assert execution.Store(config.data_root).load_run(seen["run_id"]).deadline == seen["deadline"]
        assert job_type == ("opt" if system == "water" else "sp")
        events.append("independent-reference")
        receipt = tmp_path / "synthetic-reference-receipt.json"
        receipt.write_text('{"synthetic":true}')
        monkeypatch.setattr(execution.reference.BatchLedger, "receipt_path", lambda self, ident: receipt)
        monkeypatch.setattr(execution.reference.BatchLedger, "read", lambda self, ident: {"receipt": {"synthetic": True}})
        return "synthetic-reference", {"synthetic": True}
    monkeypatch.setattr(runner, "execute", fake_execute)
    monkeypatch.setattr(execution, "_pending_geometry", fake_geometry)
    monkeypatch.setattr(execution, "_reference", fake_reference)
    monkeypatch.setattr(package, "verify_scientific_reference", lambda *a: -1.0)
    monkeypatch.setattr(execution, "_report", lambda store, run, directory: {"run_id": run.id, "synthetic": True})
    actual = execution.e2e_slot(label, system, 1, execute=True, live_model=True, live_orca=True)
    assert actual["run_id"] == seen["run_id"]
    assert events[-1] == "resume-original-pending-decision"
    state = manager[0].snapshot()["repair_cycle"]
    assert len(state["activities"]) == len(state["activity_settlements"]) == 2
    assert {a["segment"] for a in state["activities"].values()} == {"before_reference", "after_reference"}
    with pytest.raises(execution.reference.ReferenceBlocked, match="already reserved"):
        execution.e2e_slot(label, system, 1, execute=True, live_model=True, live_orca=True)
    assert len(events) == 3


def test_completed_unreviewed_model_blocks_another_slot(manager):
    _, slot = bound(manager)
    with cycle.activity(slot, seconds=0.1):
        pass
    with pytest.raises(execution.reference.ReferenceBlocked, match="awaits actual review"):
        bound(manager, cycle.MODEL_SLOTS[1])


def test_formal_source_cannot_freeze_from_unfinished_development(manager, monkeypatch):
    from tests.helpers import phase_b_bounded_package as package
    book, _, _, manifest = manager
    # Isolate prerequisite enforcement from the independent over-budget test.
    monkeypatch.setattr(cycle, "_validate_manifest", lambda *a, **k: None)
    monkeypatch.setattr(package, "_source_files", lambda: {"synthetic": "never executed"})
    with pytest.raises(execution.reference.ReferenceBlocked, match="every current development"):
        cycle.formal_freeze_requirements(cycle.candidate_label("formal", 1))
    assert manifest and not book.snapshot().get("model_records")


def test_second_formal_candidate_needs_actual_prior_failure(manager, monkeypatch):
    monkeypatch.setattr(cycle, "_validate_manifest", lambda *a, **k: None)
    with pytest.raises(execution.reference.ReferenceBlocked, match="actual first-round failure"):
        cycle.formal_freeze_requirements(cycle.candidate_label("formal", 2))
