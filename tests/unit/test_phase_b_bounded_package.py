"""Prepared live operator gates, migration and replay safety; strictly offline."""

import copy
import json
from types import SimpleNamespace

import pytest

from orca_agent.config import Config
from orca_agent.store import Store, sha256_file
from orca_agent.tools import structure
from tests.helpers import phase_b_bounded_package as package
from tests.helpers import phase_b_budget as budget
from tests.helpers.phase_b_budget_amendment import apply_approved_limits
from tests.unit.test_phase_b_budget import bind_model, record
from tests.unit.test_phase_b_budget_amendment import legacy as legacy
from tests.unit.test_structure_tools import mock_generator, response


def test_proposal_is_read_only_and_all_fixed_allocations_add_up(tmp_path, monkeypatch, capsys):
    from tests.helpers.phase_b_model_cases import variant_spec
    monkeypatch.setattr(package, "ROOT", tmp_path / "package")
    assert package.main([]) == 0
    value = json.loads(capsys.readouterr().out)
    assert value["status"] == "proposal_only"
    assert not package.ROOT.exists()
    assert sum(variant_spec(v)["budget"]["model_http_requests"] for v in package.MODEL_SLOTS) == 64
    assert sum(variant_spec(v)["budget"]["model_tokens_total"] for v in package.MODEL_SLOTS) == 512000
    assert 318 + 64 + 6 * 8 + 48 + 16 + 624 == package.reference.BOUNDED_LIMITS["model"]["http_requests"]
    assert 993584 + 512000 + 6 * 48000 + 288000 + 96000 + 4704000 == package.reference.BOUNDED_LIMITS["model"]["tokens"]
    assert package.reference.ACTIVE_LIMITS["orca_starts"]["total"] == 112


@pytest.mark.parametrize("operation", ["apply", "freeze", "model", "resolve", "prepare", "reference", "science"])
def test_unapproved_package_cannot_execute_even_with_live_switches(tmp_path, monkeypatch, operation):
    monkeypatch.setattr(package, "ROOT", tmp_path / "package")
    monkeypatch.setattr(package.reference, "BOUNDED_APPROVAL_SHA256", None)
    with pytest.raises(budget.ReferenceBlocked, match="explicit human approval"):
        package.main([operation, "--execute", "--live-model", "--live-orca", "--live-network", "--live-opi",
                      "--system", "water", "--repetition", "1", "--variant", package.DIAGNOSTICS[0]])
    assert not list(tmp_path.rglob("run.json"))


@pytest.fixture
def approved(legacy, tmp_path, monkeypatch):
    book, store, run, known, unknown, _, _ = legacy
    apply_approved_limits(book, execute=True)
    path = tmp_path / "approved.json"
    value = {"approval_id": package.reference.BOUNDED_APPROVAL_ID, "status": "user_approved",
        "previous_limits": package.reference.ACTIVE_LIMITS, "approved_limits": package.reference.BOUNDED_LIMITS,
        "previous_approval_id": package.reference.THINKING_APPROVAL_ID,
        "previous_approval_sha256": package.reference.THINKING_APPROVAL_SHA256,
        "development_package": package.scope()}
    package.reference._save(path, value)
    monkeypatch.setattr(package.reference, "BOUNDED_APPROVAL", path)
    monkeypatch.setattr(package.reference, "BOUNDED_APPROVAL_SHA256", sha256_file(path))
    return book, store, run, known, unknown


@pytest.mark.parametrize("point", [None, "after_original_snapshot", "after_amendment_receipt", "after_ledger_publication"])
def test_fourth_migration_preserves_original_costs_receipts_and_replays(approved, point):
    book, _, _, _, _ = approved
    before = book.snapshot()
    files = {p: p.read_bytes() for p in book.ledger.root.rglob("*.json") if p != book.ledger.path}
    def fault(where):
        if where == point:
            raise OSError("synthetic interrupted amendment")
    if point:
        with pytest.raises(OSError):
            package.apply_limits(execute=True, fault=fault)
    receipt = package.apply_limits(execute=True)
    after = book.snapshot()
    assert after["limits"] == package.reference.BOUNDED_LIMITS
    assert {k: v for k, v in before.items() if k not in {"limits", "limit_authority"}} == {
        k: v for k, v in after.items() if k not in {"limits", "limit_authority"}}
    assert after["model_usage"]["unknown_tokens"] == 150
    assert all(path.read_bytes() == content for path, content in files.items())
    assert package.apply_limits(execute=True) == receipt
    assert book.snapshot() == after


@pytest.mark.parametrize("target", ["approval", "before", "receipt", "lost_entry"])
def test_changed_approval_or_old_cost_cannot_grant_new_budget(approved, target):
    book, _, _, known, _ = approved
    package.apply_limits(execute=True)
    if target == "lost_entry":
        value = book.snapshot()
        del value["model_records"][known["id"]]
        package.reference._save(book.ledger.path, value)
    else:
        directory = book.ledger.root / "budget-amendments" / package.reference.BOUNDED_APPROVAL_ID
        path = (package.reference.BOUNDED_APPROVAL if target == "approval" else
                directory / ("before.json" if target == "before" else "amendment.json"))
        path.write_bytes(path.read_bytes() + b" ")
    with pytest.raises(budget.ReferenceBlocked):
        book.snapshot()


def test_scope_change_is_not_covered_by_cap_only_approval(approved):
    path = package.reference.BOUNDED_APPROVAL
    original = path.read_bytes()
    value = json.loads(original)
    value["development_package"]["repetitions"]["water_opt"] = 4
    package.reference._save(path, value)
    with pytest.raises(budget.ReferenceBlocked):
        package.apply_limits(execute=True)


def test_unknown_cost_stays_reserved_when_later_distinct_call_is_admitted(approved):
    book, store, run, _, _ = approved
    package.apply_limits(execute=True)
    value = record(2)
    book.reserve_model(run, value)
    bind_model(store, run, value)
    after = book.snapshot()
    assert after["model_usage"]["http_requests"] == 3
    assert after["model_usage"]["unknown_tokens"] == 300


@pytest.fixture
def inputs(tmp_path, monkeypatch):
    monkeypatch.setattr(package, "ROOT", tmp_path / "package")
    monkeypatch.setattr(package, "Store", lambda root: Store(root, environment_root=tmp_path / "environment"))
    monkeypatch.setattr(package, "_execution_gate", lambda **_: Config())
    monkeypatch.setattr(package, "_require_model_passes", lambda _: None)
    calls = []
    def query(_, url, **kwargs):
        name = "methane" if "/methane/" in url else "water"
        calls.append(name)
        return 200, response(name), True
    monkeypatch.setattr(structure, "_paced_query", query)
    return calls


def test_input_stage_real_dispatch_mock_transport_and_generator_reuse_exact_xyz(inputs, monkeypatch):
    for system in ("water", "methane"):
        first = package.input_stage(system, "resolve", execute=True, live=True)
        assert package.input_stage(system, "resolve", execute=True, live=True) == first
    assert inputs == ["water", "methane"]
    for system in ("water", "methane"):
        generated = mock_generator(monkeypatch, system)
        first = package.input_stage(system, "prepare", execute=True, live=True)
        before = package._prepared(system).read_bytes()
        assert package.input_stage(system, "prepare", execute=True, live=True) == first
        assert package._prepared(system).read_bytes() == before
        assert len(generated) == 1
        store, run = package._input_slot(system)
        assert run.usage.identity_queries == run.usage.structure_preparations == 1
        assert run.usage.orca_starts_actual == run.usage.model_calls == 0
        assert run.permission.scientific_execution is False
        assert store.environment_lease() is None


def test_both_identity_gates_precede_any_preparation(inputs, monkeypatch):
    package.input_stage("water", "resolve", execute=True, live=True)
    generated = mock_generator(monkeypatch)
    with pytest.raises(budget.ReferenceBlocked, match="not been executed"):
        package.input_stage("water", "prepare", execute=True, live=True)
    assert generated == []


def test_tampered_prepared_xyz_cannot_be_used_in_new_run(inputs, monkeypatch):
    for system in ("water", "methane"):
        package.input_stage(system, "resolve", execute=True, live=True)
    mock_generator(monkeypatch)
    package.input_stage("water", "prepare", execute=True, live=True)
    path = package._prepared("water")
    path.write_bytes(path.read_bytes() + b" ")
    with pytest.raises(budget.ReferenceBlocked, match="geometry changed"):
        package._prepared("water")


def test_package_unknown_input_stops_other_slots_without_weakening_old_budget(inputs):
    from orca_agent.tools.dispatch import execute_call
    store, run = package._input_slot("water")
    step = store.load_plan(run).steps[0]
    def interrupt(point):
        if point == "after_call_reserved":
            raise KeyboardInterrupt("synthetic interrupt before request")
    with pytest.raises(KeyboardInterrupt):
        execute_call(store, run, step.tool, step.parameters.model_dump(), step=step, fault=interrupt)
    with pytest.raises(budget.ReferenceBlocked, match="unknown cost/process"):
        package._no_unknown_package_cost()
    assert inputs == []


def test_prepared_copy_publication_can_recover_without_regeneration(inputs, monkeypatch):
    for system in ("water", "methane"):
        package.input_stage(system, "resolve", execute=True, live=True)
    generated = mock_generator(monkeypatch)
    result = package.input_stage("water", "prepare", execute=True, live=True)
    path = package._prepared("water")
    original = path.read_bytes()
    path.unlink()
    assert package.input_stage("water", "prepare", execute=True, live=True) == result
    assert package._prepared("water").read_bytes() == original
    assert len(generated) == 1


def test_science_raw_entry_has_frozen_prepared_input_and_no_repeat_execution(inputs, monkeypatch):
    from orca_agent import runner
    for system in ("water", "methane"):
        package.input_stage(system, "resolve", execute=True, live=True)
    for system in ("water", "methane"):
        mock_generator(monkeypatch, system)
        package.input_stage(system, "prepare", execute=True, live=True)
    monkeypatch.setattr(package.reference.BatchLedger, "read", lambda *_, **__: {"receipt":
        positive_reference(sha256_file(package._prepared("methane")))})
    package._save(package.ROOT / "candidate.json", {"offline_fixture": True})
    starts = []
    def no_execute(store, config, run_id, **kwargs):
        starts.append(run_id)
        return store.load_run(run_id)
    monkeypatch.setattr(runner, "execute", no_execute)
    result = package.science_slot("water", 1, execute=True, live_model=True, live_orca=True)
    duplicate = package.science_slot("water", 1, execute=True, live_model=True, live_orca=True)
    assert result["run_id"] == duplicate["run_id"]
    assert starts == [result["run_id"]]
    run = package.Store(package.ROOT / "science").load_run(result["run_id"])
    request = package.Store(package.ROOT / "science").load_request(run)
    assert request.normalization_status == "pending" and run.plan_id is None
    assert run.budget.orca_starts == run.budget.attempts_per_step == 1
    assert run.budget.extra_orca_starts == run.budget.identity_queries == run.budget.structure_preparations == 0
    assert run.budget.model_calls == 8 and run.budget.model_tokens == 48000
    assert run.usage.orca_starts_actual == run.usage.model_calls == 0
    grade = package.grade_science("water", 1)
    assert grade["status"] == "failed"
    assert not grade["facts"]["real_model"]
    with pytest.raises(budget.ReferenceBlocked, match="review gate"):
        package.science_slot("water", 2, execute=True, live_model=True, live_orca=True)
    assert starts == [result["run_id"]]


def test_failed_model_gate_blocks_next_slot_before_evaluation(tmp_path, monkeypatch):
    from tests.helpers import phase_b_model_evaluation as models
    monkeypatch.setattr(package, "ROOT", tmp_path / "package")
    monkeypatch.setattr(package, "_execution_gate", lambda **_: Config())
    monkeypatch.setattr(models, "regrade", lambda *_, **__: {"status": "incomplete_or_failed"})
    monkeypatch.setattr(models, "evaluate", lambda *_, **__: pytest.fail("must not spend next slot"))
    with pytest.raises(budget.ReferenceBlocked, match="not passed"):
        package.model_slot(package.DIAGNOSTICS[1], execute=True, live=True)


def test_methane_reference_mapping_is_explicit_and_does_not_weaken_water_default(tmp_path):
    from tests.unit.test_structure_tools import XYZ
    xyz, inp = tmp_path / "methane.xyz", tmp_path / "reference.inp"
    xyz.write_text(XYZ["methane"], encoding="utf-8")
    inp.write_text(package.reference.reference_input(100), encoding="utf-8")
    with pytest.raises(ValueError, match="atom mapping"):
        package.reference.reviewed_sources(xyz, inp, 100)
    sources = package.reference.reviewed_sources(xyz, inp, 100, atom_mapping=["C", "H", "H", "H", "H"])
    assert sources["geometry_sha256"] == sha256_file(xyz)
    invalid = copy.deepcopy(sources["atom_mapping"])
    invalid[0] = "N"
    with pytest.raises(ValueError, match="reviewed"):
        package.reference.reviewed_sources(xyz, inp, 100, atom_mapping=invalid)


def positive_reference(geometry_hash):
    return {"reference_verified": True, "execution_uncertain": False,
            "execution": {"state": "completed"},
            "independent_output": {"status": "converged", "energy_eh": -39.72},
            "sources": {"geometry_sha256": geometry_hash}}


@pytest.mark.parametrize("change", ["negative_reference", "failed_execution", "unverified_scf",
    "unknown", "unverified", "null_energy", "bool_energy", "nan_energy", "infinite_energy", "different_geometry"])
def test_positive_reference_gate_rejects_failures_and_wrong_prepared_input(change):
    receipt = positive_reference("exact-hash")
    if change == "negative_reference":
        receipt["execution"]["state"] = "failed"
        receipt["independent_output"] = {"status": "scf_not_converged", "energy_eh": None}
    elif change == "failed_execution":
        receipt["execution"]["state"] = "failed"
    elif change == "unverified_scf":
        receipt["independent_output"]["status"] = "unverified"
    elif change == "unknown":
        receipt["execution_uncertain"] = True
    elif change == "unverified":
        receipt["reference_verified"] = False
    elif change == "different_geometry":
        receipt["sources"]["geometry_sha256"] = "another-hash"
    else:
        receipt["independent_output"]["energy_eh"] = {
            "null_energy": None, "bool_energy": True, "nan_energy": float("nan"),
            "infinite_energy": float("inf")}[change]
    original = copy.deepcopy(receipt)
    with pytest.raises(budget.ReferenceBlocked, match="positive|geometry"):
        package.verify_scientific_reference(receipt, "exact-hash")
    assert repr(receipt) == repr(original)


def test_positive_reference_gate_returns_only_the_finite_independent_energy():
    receipt = positive_reference("exact-hash")
    assert package.verify_scientific_reference(receipt, "exact-hash") == -39.72
    receipt["independent_output"]["energy_eh"] = 0
    assert package.verify_scientific_reference(receipt, "exact-hash") == 0


def test_verified_negative_reference_blocks_science_before_slot_reservation(tmp_path, monkeypatch):
    root = tmp_path / "package"
    xyz = tmp_path / "geometry.xyz"
    xyz.write_bytes(b"prepared fixture")
    receipt = positive_reference(sha256_file(xyz))
    receipt["execution"].update(state="failed", reason="nonzero_exit_code")
    receipt["independent_output"] = {"status": "scf_not_converged", "energy_eh": None}
    monkeypatch.setattr(package, "ROOT", root)
    monkeypatch.setattr(package, "_execution_gate", lambda **_: Config())
    monkeypatch.setattr(package, "_require_model_passes", lambda _: None)
    monkeypatch.setattr(package, "_prepared", lambda _: xyz)
    monkeypatch.setattr(package.reference.BatchLedger, "read", lambda *_: {"receipt": receipt})
    monkeypatch.setattr(package, "_save", lambda *_: pytest.fail("must not reserve a science slot"))
    with pytest.raises(budget.ReferenceBlocked, match="positive"):
        package.science_slot("water", 1, execute=True, live_model=True, live_orca=True)
    assert not root.exists()


@pytest.mark.parametrize("positive", [False, True])
def test_methane_reference_forwards_frozen_config_and_requires_positive_result(tmp_path, monkeypatch, positive):
    xyz = tmp_path / "geometry.xyz"
    xyz.write_bytes(b"prepared fixture")
    frozen = Config(orca_path=tmp_path / "frozen-orca.exe", mpi_path=tmp_path / "frozen-mpi.exe")
    receipt = positive_reference(sha256_file(xyz))
    if not positive:
        receipt["execution"]["state"] = "failed"
        receipt["independent_output"] = {"status": "scf_not_converged", "energy_eh": None}
    monkeypatch.setattr(package, "_execution_gate", lambda **_: frozen)
    monkeypatch.setattr(package, "_require_model_passes", lambda _: None)
    monkeypatch.setattr(package, "_prepared", lambda _: xyz)
    observed = []
    def capture(*args, **kwargs):
        observed.append(kwargs.get("config"))
        return receipt
    monkeypatch.setattr(package.reference, "execute_reference", capture)
    if positive:
        assert package.methane_reference(execute=True, live=True) == receipt
    else:
        with pytest.raises(budget.ReferenceBlocked, match="positive"):
            package.methane_reference(execute=True, live=True)
    assert observed == [frozen]


@pytest.mark.parametrize("use_frozen_config", [False, True])
def test_reference_actual_launch_config_cannot_override_frozen_binaries(tmp_path, monkeypatch, use_frozen_config):
    reference = package.reference
    from tests.unit.test_structure_tools import XYZ
    xyz, inp = tmp_path / "geometry.xyz", tmp_path / "reference.inp"
    xyz.write_text(XYZ["methane"], encoding="utf-8")
    inp.write_text(reference.reference_input(100), encoding="utf-8")
    frozen = Config(orca_path=tmp_path / "frozen-orca.exe", mpi_path=tmp_path / "frozen-mpi.exe",
                    data_root=tmp_path / "candidate-data")
    monkeypatch.setenv("ORCA_AGENT_ORCA", str(tmp_path / "other-orca.exe"))
    monkeypatch.setenv("ORCA_AGENT_MPI", str(tmp_path / "other-mpi.exe"))
    monkeypatch.setattr(reference, "BATCH_ROOT", tmp_path / "batch")
    receipts, configs = [], []
    ledger = SimpleNamespace(reserve=lambda *_: ({"fingerprint": "offline-fixture"}, True),
                             finish=lambda _, receipt: receipts.append(receipt))
    monkeypatch.setattr(reference, "BatchLedger", lambda: ledger)
    monkeypatch.setattr(reference, "Store", lambda root: SimpleNamespace(root=root))
    def stop_before_initialization(store, config, path):
        configs.append(config)
        raise RuntimeError("offline sentinel before Run initialization or execution")
    monkeypatch.setattr(reference.runner, "initialize", stop_before_initialization)
    kwargs = {"config": frozen} if use_frozen_config else {}
    reference.execute_reference("offline-config", "reference", xyz, inp, 100,
                                atom_mapping=["C", "H", "H", "H", "H"], **kwargs)
    assert len(configs) == len(receipts) == 1
    assert configs[0].orca_path == (frozen.orca_path if use_frozen_config else tmp_path / "other-orca.exe")
    assert configs[0].mpi_path == (frozen.mpi_path if use_frozen_config else tmp_path / "other-mpi.exe")
    assert configs[0].data_root == tmp_path / "batch" / "reference"
    assert frozen.data_root == tmp_path / "candidate-data"
    assert receipts[0]["usage"]["orca_starts_actual"] == 0
