import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "helpers" / "phase_b_reference.py"
spec = importlib.util.spec_from_file_location("phase_b_reference", SCRIPT)
reference = importlib.util.module_from_spec(spec)
spec.loader.exec_module(reference)
FIXTURES = SCRIPT.parents[1] / "fixtures" / "phase_a"


@pytest.fixture
def ledger(tmp_path, monkeypatch):
    monkeypatch.setattr(reference, "BATCH_ROOT", tmp_path / "batch")
    monkeypatch.setattr(reference, "DELIVERED_SNAPSHOT", tmp_path / "delivered-snapshot.json")
    return reference.BatchLedger()


def sources(number=0):
    return {"input_sha256": f"input-{number}", "geometry_sha256": "geometry",
            "parameters": {"scf_maxiter": 100}}


def receipt(entry, **extra):
    return {"id": entry["id"], "category": entry["category"],
            "run_id": entry["run_id"], "fingerprint": entry["fingerprint"],
            "execution_uncertain": False,
            "evidence_files": [], "usage": {"orca_starts_actual": 1}, **extra}


def test_read_only_does_not_create_ledger(ledger, capsys):
    assert reference.main([]) == 0
    assert json.loads(capsys.readouterr().out)["limits"] == reference.ACTIVE_LIMITS
    assert not ledger.root.exists()


def test_missing_durable_ledger_cannot_reset_delivered_batch(ledger, tmp_path, monkeypatch):
    reference._save(reference.DELIVERED_SNAPSHOT, {
        "schema_version": 1, "limits": reference.LIMITS,
        "entries": {f"reference-{i}": {"category": "reference"} for i in range(16)},
    })
    with pytest.raises(reference.ReferenceBlocked, match="restore the original batch archive"):
        reference.main([])
    with pytest.raises(reference.ReferenceBlocked, match="budgets cannot restart"):
        ledger.reserve("new-reference", "reference", sources())
    inp = tmp_path / "reference.inp"
    inp.write_text(reference.reference_input(100), encoding="utf-8")
    monkeypatch.setattr(reference.runner, "initialize", lambda *args: pytest.fail(
        "missing-ledger guard must precede even reference initialization"))
    with pytest.raises(reference.ReferenceBlocked, match="budgets cannot restart"):
        reference.execute_reference("new-reference", "reference",
            FIXTURES / "water_sp" / "geometry.xyz", inp, 100)
    assert not ledger.root.exists()


def test_existing_durable_ledger_retains_usage_when_delivered_snapshot_exists(ledger):
    ledger.reserve("existing", "reference", sources())
    before = ledger.path.read_bytes()
    reference._save(reference.DELIVERED_SNAPSHOT, ledger.snapshot())
    assert set(ledger.snapshot()["entries"]) == {"existing"}
    assert ledger.path.read_bytes() == before


@pytest.mark.parametrize("category,limit", [("reference", 16), ("development", 48)])
def test_reservations_include_unknown_and_cannot_exceed_subcap(ledger, category, limit):
    for index in range(limit):
        ledger.reserve(f"r{index}", category, sources(index))
    with pytest.raises(reference.ReferenceBlocked, match="limit exhausted"):
        ledger.reserve("overflow", category, sources(limit))
    assert len(ledger.snapshot()["entries"]) == limit


def test_completed_replay_does_not_reserve_or_execute(ledger):
    entry, fresh = ledger.reserve("same", "reference", sources())
    assert fresh
    saved = receipt(entry)
    ledger.finish("same", saved)
    before = ledger.path.read_bytes()
    replay, fresh = ledger.reserve("same", "reference", sources())
    assert not fresh and replay == saved
    assert ledger.path.read_bytes() == before


@pytest.mark.parametrize("category,source", [
    ("development", sources()), ("reference", sources(1)),
    ("reference", {**sources(), "parameters": {"scf_maxiter": 2}}),
])
def test_same_id_cannot_change_frozen_binding(ledger, category, source):
    ledger.reserve("same", "reference", sources())
    with pytest.raises(reference.ReferenceBlocked, match="cannot change"):
        ledger.reserve("same", category, source)


def test_unknown_cannot_replay_or_change_id(ledger):
    entry, _ = ledger.reserve("one", "reference", sources())
    with pytest.raises(reference.ReferenceBlocked, match="no receipt"):
        ledger.reserve("one", "reference", sources())
    with pytest.raises(reference.ReferenceBlocked, match="unresolved"):
        ledger.reserve("two", "development", sources())
    ledger.finish("one", receipt(entry, execution_uncertain=True))
    with pytest.raises(reference.ReferenceBlocked, match="unresolved"):
        ledger.reserve("two", "development", sources())
    assert ledger.reserve("one", "reference", sources())[1] is False


def test_receipt_publication_crash_never_resends(ledger, monkeypatch):
    entry, _ = ledger.reserve("one", "reference", sources())
    original = reference._save

    def crash(path, value, **kwargs):
        if path == ledger.path:
            raise OSError("crash after immutable receipt")
        return original(path, value, **kwargs)

    monkeypatch.setattr(reference, "_save", crash)
    with pytest.raises(OSError):
        ledger.finish("one", receipt(entry))
    replay, fresh = ledger.reserve("one", "reference", sources())
    assert fresh is False
    assert replay["execution_uncertain"] is True
    assert replay["reference_verified"] is False
    assert replay["receipt_publication"] == "uncommitted_read_only"
    with pytest.raises(reference.ReferenceBlocked, match="unresolved"):
        ledger.reserve("new-id", "development", sources())


@pytest.mark.parametrize("operation", ["snapshot", "read", "replay", "different_id"])
def test_changed_receipt_is_rejected_before_it_can_be_used(ledger, operation):
    entry, _ = ledger.reserve("one", "reference", sources())
    ledger.finish("one", receipt(entry, execution_uncertain=True, reference_verified=False))
    path = ledger.receipt_path("one")
    changed = json.loads(path.read_text(encoding="utf-8"))
    changed.update(execution_uncertain=False, reference_verified=True, evidence_files=[])
    reference._save(path, changed)
    operations = {
        "snapshot": ledger.snapshot,
        "read": lambda: ledger.read("one"),
        "replay": lambda: ledger.reserve("one", "reference", sources()),
        "different_id": lambda: ledger.reserve("another", "development", sources()),
    }
    with pytest.raises(reference.ReferenceBlocked, match="receipt changed"):
        operations[operation]()


@pytest.mark.parametrize("changed", [{"id": "other"}, {"run_id": "run_other"},
                                     {"fingerprint": "wrong"}, {"category": "development"}])
def test_uncommitted_receipt_requires_exact_binding(ledger, changed):
    ledger.reserve("one", "reference", sources())
    ledger.bind_run("one", "run_one")
    entry = ledger.snapshot()["entries"]["one"]
    reference._save(ledger.receipt_path("one"), receipt(entry, **changed))
    with pytest.raises(reference.ReferenceBlocked, match="identity/run binding"):
        ledger.reserve("one", "reference", sources())
    with pytest.raises(reference.ReferenceBlocked, match="identity/run binding"):
        ledger.read("one")
    with pytest.raises(reference.ReferenceBlocked, match="unresolved"):
        ledger.reserve("different", "development", sources())


@pytest.mark.parametrize("changed", [{"id": "other"}, {"run_id": "run_other"}])
def test_wrong_receipt_identity_cannot_be_published(ledger, changed):
    entry, _ = ledger.reserve("one", "reference", sources())
    with pytest.raises(reference.ReferenceBlocked, match="identity/run binding"):
        ledger.finish("one", receipt(entry, **changed))
    assert not ledger.receipt_path("one").exists()


def test_known_completed_repeat_under_new_id_still_consumes_batch_quota(ledger):
    entry, _ = ledger.reserve("one", "reference", sources())
    ledger.finish("one", receipt(entry))
    _, fresh = ledger.reserve("different", "development", sources())
    assert fresh is True
    assert len(ledger.snapshot()["entries"]) == 2


def test_unbound_receipt_cannot_be_read_as_reference(ledger):
    reference._save(ledger.receipt_path("orphan"), {"reference_verified": True})
    with pytest.raises(reference.ReferenceBlocked, match="no matching reservation"):
        ledger.read("orphan")


def test_changed_evidence_is_rejected_on_replay(ledger, tmp_path):
    evidence = tmp_path / "stdout.out"
    evidence.write_text("original")
    entry, _ = ledger.reserve("one", "reference", sources())
    ledger.finish("one", receipt(entry, evidence_files=[{
        "path": str(evidence), "sha256": reference.sha256_file(evidence)}]))
    evidence.write_text("changed")
    with pytest.raises(reference.ReferenceBlocked, match="evidence changed"):
        ledger.reserve("one", "reference", sources())


def test_run_binding_is_immutable(ledger):
    ledger.reserve("one", "reference", sources())
    ledger.bind_run("one", "run_one")
    assert ledger.snapshot()["entries"]["one"]["run_id"] == "run_one"
    with pytest.raises(reference.ReferenceBlocked):
        ledger.bind_run("one", "run_two")


def test_total_limit_counts_all_categories(ledger):
    data = ledger.snapshot()
    data["entries"] = {f"historical-{i}": {
        "category": "formal", "fingerprint": f"other-{i}"} for i in range(reference.LIMITS["orca_starts"]["total"])}
    reference._save(ledger.path, data)
    with pytest.raises(reference.ReferenceBlocked, match="limit exhausted"):
        ledger.reserve("new-reference", "reference", sources())


def test_initialization_failure_keeps_reservation_and_receipt(ledger, tmp_path, monkeypatch):
    inp = tmp_path / "reference.inp"
    inp.write_text(reference.reference_input(100))
    calls = []

    def fail(*args):
        calls.append("initialize")
        raise ValueError("sensitive diagnostic is intentionally omitted")

    monkeypatch.setattr(reference.runner, "initialize", fail)
    result = reference.execute_reference("init-failed", "reference",
        FIXTURES / "water_sp" / "geometry.xyz", inp, 100)
    assert result["exception"] == {"type": "ValueError", "stage": "initialization"}
    assert result["execution_uncertain"] is False
    assert result["usage"]["orca_starts_actual"] == 0
    assert len(ledger.snapshot()["entries"]) == 1
    before = ledger.path.read_bytes()
    replay = reference.execute_reference("init-failed", "reference",
        FIXTURES / "water_sp" / "geometry.xyz", inp, 100)
    assert replay == result and calls == ["initialize"]
    assert ledger.path.read_bytes() == before
    assert "sensitive" not in ledger.receipt_path("init-failed").read_text(encoding="utf-8")


def test_execution_exception_preserves_run_binding_and_unknown_cost(ledger, tmp_path, monkeypatch):
    inp = tmp_path / "reference.inp"
    inp.write_text(reference.reference_input(2))
    run = SimpleNamespace(id="run_test", attempts=[], state="unknown", result_ids=[],
                          usage=SimpleNamespace(model_dump=lambda **kw: {"orca_starts_actual": 0}))

    class FakeStore:
        def __init__(self, root):
            self.root = root

        def path(self, value):
            return self.root / value

        def load_run(self, run_id):
            assert run_id == run.id
            return run

    def initialize(store, config, spec):
        reference._save(store.path(f"runs/{run.id}/environment.json"), {"test": "offline"})
        reference._save(store.path(f"runs/{run.id}/run.json"), {"id": run.id})
        return run

    def interrupted_execution(store, config, run_id):
        assert ledger.snapshot()["entries"]["crash"]["run_id"] == run_id
        raise RuntimeError("lost execution response")

    monkeypatch.setattr(reference, "Store", FakeStore)
    monkeypatch.setattr(reference.runner, "initialize", initialize)
    monkeypatch.setattr(reference.runner, "execute", interrupted_execution)
    result = reference.execute_reference("crash", "reference",
        FIXTURES / "water_sp" / "geometry.xyz", inp, 2)
    assert result["run_id"] == run.id
    assert result["execution_uncertain"] is True
    assert result["exception"] == {"type": "RuntimeError", "stage": "execution"}
    assert ledger.snapshot()["entries"]["crash"]["orca_starts_reserved"] == 1
    with pytest.raises(reference.ReferenceBlocked, match="unresolved"):
        reference.execute_reference("new-id", "development",
            FIXTURES / "water_sp" / "geometry.xyz", inp, 2)


def test_exact_frozen_input_bytes_and_source_conflict(tmp_path):
    inp = tmp_path / "reference.inp"
    inp.write_text(reference.reference_input(2), encoding="utf-8")
    geometry = FIXTURES / "water_sp" / "geometry.xyz"
    reviewed = reference.reviewed_sources(geometry, inp, 2)
    prepare = reference._frozen_input(reviewed)
    workdir = tmp_path / "attempt"
    workdir.mkdir()
    parameters = reference.CalculationParameters(scf_maxiter=2, timeout_seconds=120)
    prepare(workdir, geometry, parameters, "orca.sp")
    assert (workdir / "job.inp").read_bytes() == inp.read_bytes()
    assert (workdir / "geometry.xyz").read_bytes() == geometry.read_bytes()
    inp.write_text(reference.reference_input(100), encoding="utf-8")
    with pytest.raises(ValueError, match="bytes changed"):
        prepare(tmp_path / "other", geometry, parameters, "orca.sp")


def test_profile_rejects_extra_execution_controls(tmp_path):
    inp = tmp_path / "reference.inp"
    inp.write_text(reference.reference_input(2) + "%output jsonpropertyfile true end\n")
    with pytest.raises(ValueError, match="SP-only"):
        reference.reviewed_sources(FIXTURES / "water_sp" / "geometry.xyz", inp, 2)


def test_independent_raw_success_and_real_failure():
    success = reference.independent_output(FIXTURES / "real_water_sp" / "stdout.out")
    assert success["status"] == "converged"
    assert success["energy_eh"] == pytest.approx(-74.962991615317, abs=1e-12)
    assert success["energy_line"] > success["scf_converged_line"]
    assert success["print_rounding_eh"] > 0
    failure = reference.independent_output(FIXTURES / "real_water_scf_limit" / "stdout.out")
    assert failure["status"] == "scf_not_converged"
    assert failure["energy_eh"] is None
    assert failure["iterations"][-1]["number"] == 1


@pytest.mark.parametrize("replace", [
    ("ORCA TERMINATED NORMALLY", "ABNORMAL STOP"),
    ("SCF CONVERGED AFTER", "SCF INCOMPLETE AFTER"),
    ("Your calculation utilizes the basis: STO-3G", "Your calculation utilizes the basis: UNKNOWN"),
])
def test_raw_energy_without_sufficient_evidence_is_not_reference(tmp_path, replace):
    path = tmp_path / "stdout.out"
    path.write_text((FIXTURES / "real_water_sp" / "stdout.out").read_text().replace(*replace))
    assert reference.independent_output(path)["status"] == "unverified"


def test_failure_requires_actual_iteration_evidence(tmp_path):
    path = tmp_path / "stdout.out"
    text = (FIXTURES / "real_water_scf_limit" / "stdout.out").read_text()
    path.write_text("\n".join(line for line in text.splitlines()
                              if "-74.9039506677429046" not in line))
    assert reference.independent_output(path)["status"] == "unverified"


@pytest.mark.parametrize("newline", ["\n", "\r\n", "\r\r\n"])
@pytest.mark.parametrize("success", [True, False])
def test_raw_line_locations_skip_leading_blank_records(tmp_path, newline, success):
    records = [
        "Hartree-Fock type HFTyp .... RHF",
        "Your calculation utilizes the basis: STO-3G",
        "Total Charge Charge .... 0",
        "Multiplicity Mult .... 1",
        "", "   ",
    ]
    if success:
        records += ["    *** SCF CONVERGED AFTER 4 CYCLES", "", "  ",
                    "    FINAL SINGLE POINT ENERGY -74.962991615317", "",
                    "ORCA TERMINATED NORMALLY"]
    else:
        records += ["    1 -74.9039506677429046 0.0 0.1 0.1 0.1 0.7 0.2", "", "  ",
                    "    2 -74.9218236449052881 -0.1 0.1 0.1 0.1 0.7 0.3", "",
                    "    SCF NOT CONVERGED AFTER 1 CYCLES"]
    path = tmp_path / "stdout.out"
    payload = (newline.join(records) + newline).encode("utf-8")
    path.write_bytes(payload)
    result = reference.independent_output(path)
    assert path.read_bytes() == payload
    assert result["line_numbering"] == "1-based LF-delimited raw UTF-8 records; CR retained"
    if success:
        assert result["status"] == "converged"
        assert result["scf_converged_line"] == 7
        assert result["energy_line"] == 10
        assert "FINAL SINGLE POINT ENERGY" in payload.decode().split("\n")[9]
    else:
        assert result["status"] == "scf_not_converged"
        assert result["iterations"] == [{"number": 1, "line": 7}, {"number": 2, "line": 10}]
        assert result["failure_line"] == 12
        assert "SCF NOT CONVERGED" in payload.decode().split("\n")[11]
