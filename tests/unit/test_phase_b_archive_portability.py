"""Synthetic legacy Store restoration; no historical or live scientific claim."""

import json

import pytest

from orca_agent.models import (
    Attempt,
    Check,
    Goal,
    PermissionSnapshot,
    QualifiedOutput,
    Request,
    Result,
    utc_now,
)
from orca_agent.store import Store, sha256_file
from tests.helpers import phase_b_model_cases as cases


@pytest.mark.parametrize("case_name,final_geometry", [("water_sp", False), ("methane_opt", True)])
def test_synthetic_legacy_restore_keeps_every_byte_rule_and_cost(tmp_path, monkeypatch, case_name, final_geometry):
    project = tmp_path / "synthetic-checkout"
    source = Store(project / "data")
    raw = tmp_path / "geometry.xyz"
    raw.write_bytes(b"1\r\nSynthetic fixture, not ORCA evidence\r\nH 0 0 0\r\n")
    initial = source.import_artifact(raw, "synthetic_initial_geometry")
    request = Request(original_text="Synthetic old archive for byte-preservation testing only.",
                      geometry_artifact_id=initial.id,
                      goals=[Goal(id="old_energy", port="energy", minimum_check_version="orca-hf-1")])
    legacy = source.create_run(request, None, PermissionSnapshot(
        allowed_tools=[], artifact_ids=[initial.id]))
    attempt = Attempt(step_id="synthetic_step", logical_id="synthetic_step", number=1,
        tool="orca.opt" if final_geometry else "orca.sp", state="completed", started=False,
        geometry_artifact_id=initial.id, input_fingerprint="synthetic-not-calculated",
        directory=f"runs/{legacy.id}/attempts/synthetic", finished_at=utc_now())
    # These are injected accounting sentinels, never claims of actual calls.
    legacy.attempts.append(attempt)
    legacy.usage.model_calls = 2
    legacy.usage.model_tokens_used = 123
    legacy.usage.model_tokens_unknown = 45
    legacy.state = "completed"
    source.save_run(legacy)
    files = {"geometry.xyz": {"artifact_id": initial.id, "sha256": initial.sha256}}
    evidence = []
    contents = {"stdout.out": "Synthetic old output. 不是真实 ORCA。\r\n".encode(),
                "job.inp": b"# Synthetic old input\r\n# no executable calculation\n"}
    if final_geometry:
        contents["job.xyz"] = b"1\nSynthetic final geometry\nH 0 0 1\n"
    for name, content in contents.items():
        path = tmp_path / name
        path.write_bytes(content)
        artifact = source.import_artifact(path, "synthetic_legacy_evidence",
                                          run_id=legacy.id, attempt_id=attempt.id)
        evidence.append(artifact.id)
        files[name] = {"artifact_id": artifact.id, "sha256": artifact.sha256}
    check = Check(name="synthetic_old_check", status="passed", rule_version="orca-hf-1",
                  detail="Injected fixture; no scientific check performed.")
    result = Result(run_id=legacy.id, step_id=attempt.step_id, attempt_id=attempt.id,
        operation_status="completed", checks={"energy": [check]}, artifact_ids=evidence,
        qualified_outputs={"energy": QualifiedOutput(value=-1.25, unit="Eh", checks=[check])},
        source={"files": files, "conditions": dict(cases.PHYSICAL), "test_kind": "synthetic"})
    source.save_result(result)
    legacy.result_ids.append(result.id)
    attempt.result_id = result.id
    source.save_run(legacy)

    receipt_path = project / "synthetic-receipt.json"
    receipt = {"store_root": str(source.root), "run_id": legacy.id, "result_ids": [result.id],
               "test_kind": "synthetic_not_historical_scientific_evidence"}
    receipt_path.write_bytes(json.dumps(receipt, ensure_ascii=False, indent=1).encode() + b"\r\n")
    index = project / "docs/acceptance/phase-a/evidence-index.json"
    index.parent.mkdir(parents=True)
    index.write_text(json.dumps({"current_cases": {case_name: {"receipt": receipt,
        "receipt_path": str(receipt_path), "receipt_sha256": sha256_file(receipt_path)}}}), encoding="utf-8")
    monkeypatch.setattr(cases, "PROJECT", project)
    monkeypatch.setattr(Store, "reserve_attempt", lambda *a, **k: pytest.fail("restore must never reserve science"))
    monkeypatch.setattr(Store, "reserve_call", lambda *a, **k: pytest.fail("restore must never execute a Tool"))
    destination = Store(tmp_path / "fresh-evaluation-store")
    current = destination.create_run(Request(goals=[Goal(id="current", port="unresolved")]), None)
    current_before = destination.path(f"runs/{current.id}/run.json").read_bytes()
    source_before = {path.relative_to(source.root).as_posix(): path.read_bytes()
                     for path in source.root.rglob("*") if path.is_file()}

    for _ in range(2):  # Existing identical targets are reused, never rewritten.
        metadata = {"fixture_gaps": [], "archive_imports": [], "artifact_ids": []}
        visible = cases._legacy_reference(destination, case_name, metadata)
        restored = metadata["archive_imports"][0]
        assert not metadata["fixture_gaps"]
        assert visible["observed_historical_output"] == {
            "value": -1.25, "unit": "Eh", "scientific_status": "not_verified_for_current_rule",
            "rule_versions": ["orca-hf-1"]}
        assert "qualified" not in visible and restored["rule_versions"] == ["orca-hf-1"]
        assert visible["geometry_artifact_id"] == files["job.xyz" if final_geometry else "geometry.xyz"]["artifact_id"]
        assert destination.load_result(legacy.id, result.id) == result
        assert destination.load_run(legacy.id).model_dump() == legacy.model_dump()
        members = {item["relative_path"]: item["sha256"] for item in restored["manifest"]}
        assert set(members) == {name for name in source_before if name.startswith((f"runs/{legacy.id}/", "artifacts/"))}
        for name, digest in members.items():
            assert source.path(name).read_bytes() == destination.path(name).read_bytes() == source_before[name]
            assert sha256_file(source.path(name)) == sha256_file(destination.path(name)) == digest
        assert destination.path(f"runs/{current.id}/run.json").read_bytes() == current_before
        assert destination.load_run(current.id).usage.orca_starts_reserved == 0
        assert destination.load_run(current.id).usage.model_calls == 0
    assert {path.relative_to(source.root).as_posix(): path.read_bytes()
            for path in source.root.rglob("*") if path.is_file()} == source_before
