"""One fixed, explicitly approved development package; never a new budget ledger.

Default invocation only prints the proposed scope. Real operations require a
pinned human approval, the original migrated ledger, an immutable source freeze,
and the operation's explicit live switch. Failed/interrupted slots are retained.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import importlib.metadata
import json
import math
import subprocess
from pathlib import Path

from filelock import FileLock

from orca_agent.models import (
    BudgetLimits,
    EvidenceRef,
    Goal,
    OutputBinding,
    PermissionSnapshot,
    Plan,
    Request,
    Step,
    SystemInput,
    utc_now,
)
from orca_agent.store import Store, atomic_write, sha256_file
from tests.helpers import phase_b_budget as budget
from tests.helpers import phase_b_freeze as freeze

reference = budget.reference
PROJECT = reference.PROJECT
ROOT = reference.BATCH_ROOT / "bounded-20261008"
LABEL = "bounded-20261008"
DIAGNOSTICS = (
    "N-06/raw-unsupported-system", "V-06/insufficient-additional-budget",
    "V-07/array-location", "V-09/different-method", "V-09/missing-electron-state",
)
RAW_GATES = (
    "N-03/raw-electron-state-clarification", "N-03/raw-ambiguous-reference",
    "N-04/raw-authorized-defaults", "N-04/raw-unique-inheritance",
    "N-04/raw-unconfirmed-inference", "N-07/raw-read-only-window",
    "N-09/raw-user-goal-replacement", "N-09/raw-preserve-goal",
)
MODEL_SLOTS = DIAGNOSTICS + RAW_GATES
WATER_REFERENCE = PROJECT / "data/acceptance/reference-water_opt.json"
WATER_REFERENCE_SHA256 = "01f0124270697f2df3d1509a07fafc8c9cff97eff5849b5d39564c5afdbc5a3f"
REFERENCE_ID = "bounded-20261008-methane-prepared-sp"


def scope():
    return {
        "schema_version": 1, "package_id": LABEL, "model_profile": "disabled",
        "diagnostics": list(DIAGNOSTICS), "raw_gates": list(RAW_GATES),
        "repetitions": {"water_opt": 3, "methane_prepared_sp": 3},
        "new_orca_starts": {"reference": 1, "development": 6},
        "maximum_model_usage": {"http_requests": 112, "tokens": 800000},
        "structure": {"identity_queries": 2, "structure_preparations": 2,
                      "systems": ["water", "methane"], "retries": 0,
                      "prepare_seconds": 30, "prepare_cores": 1, "prepare_memory_mb": 1024,
                      "response_bytes": 262144, "response_deadline_seconds": 20,
                      "http_read_timeout_seconds": 10, "http_connect_timeout_seconds": 5},
        "previous_limits": reference.ACTIVE_LIMITS, "proposed_limits": reference.BOUNDED_LIMITS,
        "preserved_old_remaining": {"C": {"http_requests": 48, "tokens": 288000, "orca_starts": 16},
            "conditional_D": {"http_requests": 16, "tokens": 96000, "orca_starts": 6},
            "formal": {"http_requests": 624, "tokens": 4704000, "orca_starts": 48}},
        "water_reference_sha256": WATER_REFERENCE_SHA256,
        "reference_id": REFERENCE_ID,
        "limitations": ["prepared XYZ is generated once per system, reused by three independent model/science Runs",
            "input acquisition exercises production Tools without model-selected acquisition",
            "not a formal acceptance freeze; historical Results and quota remain unchanged"],
    }


def _approval():
    value = reference.bounded_approval()
    if value.get("development_package") != scope():
        raise reference.ReferenceBlocked("approved package scope differs from this exact operator")
    return value


def _save(path, value):
    reference._save(path, value, immutable=True)


def apply_limits(*, execute=False, fault=None):
    """Append fourth approval to the existing ledger; preserve every old receipt."""
    if not execute:
        raise reference.ReferenceBlocked("migration requires explicit --execute")
    approval = _approval()
    book = budget.AcceptanceBudget(Store(reference.BATCH_ROOT / "reference"))
    ledger = book.ledger
    with ledger._lock():
        if not ledger.path.is_file():
            raise reference.ReferenceBlocked("original ledger is required; no empty replacement")
        before = book._snapshot_unlocked()
        directory = ledger.root / "budget-amendments" / reference.BOUNDED_APPROVAL_ID
        receipt_path, before_path = directory / "amendment.json", directory / "before.json"
        if before["limits"] == reference.BOUNDED_LIMITS:
            return reference._json(receipt_path)
        if (before["limits"] != reference.ACTIVE_LIMITS
                or before.get("limit_authority", {}).get("origin") != "amendment"):
            raise reference.ReferenceBlocked("source is not the existing third approved batch")
        baseline = approval.get("approval_baseline", {}).get("ledger_sha256")
        if baseline and sha256_file(ledger.path) != baseline:
            raise reference.ReferenceBlocked("approved spend baseline changed; recheck preserved scope before migration")
        original = ledger.path.read_bytes()
        if before_path.exists():
            if before_path.read_bytes() != original:
                raise reference.ReferenceBlocked("interrupted amendment baseline changed; reconcile")
        else:
            atomic_write(before_path, original, immutable=True)
        if fault:
            fault("after_original_snapshot")
        immutable = {"schema_version": 1, "approval_id": reference.BOUNDED_APPROVAL_ID,
            "approval_sha256": reference.BOUNDED_APPROVAL_SHA256,
            "previous_limits": reference.ACTIVE_LIMITS, "approved_limits": reference.BOUNDED_LIMITS,
            "previous_limit_authority": before["limit_authority"], "before_sha256": sha256_file(before_path),
            "preserved_model_usage": before["model_usage"],
            "preserved_entry_counts": {kind: len(before.get(kind, {}))
                                       for kind in ("entries", "model_records", "agent_science")}}
        if receipt_path.exists():
            receipt = reference._json(receipt_path)
            if {k: v for k, v in receipt.items() if k != "applied_at"} != immutable:
                raise reference.ReferenceBlocked("interrupted amendment differs from approval")
        else:
            receipt = {**immutable, "applied_at": utc_now().isoformat()}
            _save(receipt_path, receipt)
        if fault:
            fault("after_amendment_receipt")
        updated = copy.deepcopy(before)
        updated["limits"] = copy.deepcopy(reference.BOUNDED_LIMITS)
        updated["limit_authority"] = {"approval_id": reference.BOUNDED_APPROVAL_ID,
            "approval_sha256": reference.BOUNDED_APPROVAL_SHA256, "origin": "amendment",
            "receipt_sha256": sha256_file(receipt_path)}
        reference._save(ledger.path, updated)
        if fault:
            fault("after_ledger_publication")
        after = book._snapshot_unlocked()
        if {k: v for k, v in before.items() if k not in {"limits", "limit_authority"}} != {
                k: v for k, v in after.items() if k not in {"limits", "limit_authority"}}:
            raise reference.ReferenceBlocked("amendment changed existing accounting")
        return receipt


def _source_files():
    return {name: sha256_file(PROJECT / name) for name in freeze.execution_files()}


def freeze_candidate():
    """Read executable/dependency identities only; never execute ORCA or OPI."""
    _approval()
    if subprocess.check_output(["git", "status", "--porcelain"], cwd=PROJECT, text=True).strip():
        raise reference.ReferenceBlocked("candidate freeze requires a clean committed checkout")
    config = freeze.evaluation_config(science=True, model_profile="disabled")
    runtime = freeze.runtime_environment()
    runtime["rdkit_version"] = importlib.metadata.version("rdkit")
    binaries = {name: {"path": str(path), "sha256": sha256_file(path)}
                for name, path in (("orca", config.orca_path), ("mpi", config.mpi_path)) if path is not None}
    if set(binaries) != {"orca", "mpi"}:
        raise reference.ReferenceBlocked("candidate requires configured ORCA and MPI file identities")
    value = {"scope": scope(), "source_files": _source_files(), "runtime": runtime, "binaries": binaries,
             "configuration": config.model_dump(mode="json"),
             "commit": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=PROJECT, text=True).strip(),
             "approval_sha256": reference.BOUNDED_APPROVAL_SHA256}
    path = ROOT / "candidate.json"
    if path.exists():
        if reference._json(path) != value:
            raise reference.ReferenceBlocked("candidate already frozen; no silent replacement/new identity")
    else:
        _save(path, value)
    return value


def _execution_gate(*, execute, live):
    if not execute or not live:
        raise reference.ReferenceBlocked("operation requires --execute and its explicit live switch")
    _approval()
    record = reference._json(ROOT / "candidate.json")
    runtime = freeze.runtime_environment()
    runtime["rdkit_version"] = importlib.metadata.version("rdkit")
    config = freeze.evaluation_config(science=True, model_profile="disabled")
    if (record.get("scope") != scope() or record.get("source_files") != _source_files()
            or record.get("runtime") != runtime or record.get("configuration") != config.model_dump(mode="json")
            or record.get("approval_sha256") != reference.BOUNDED_APPROVAL_SHA256):
        raise reference.ReferenceBlocked("candidate/source/schema/prompt/profile changed; stop this package")
    for item in record["binaries"].values():
        if sha256_file(Path(item["path"])) != item["sha256"]:
            raise reference.ReferenceBlocked("frozen executable changed")
    ledger = budget.AcceptanceBudget(Store(reference.BATCH_ROOT / "reference")).snapshot()
    if ledger["limits"] != reference.BOUNDED_LIMITS:
        raise reference.ReferenceBlocked("explicit fourth budget migration has not been applied")
    _no_unknown_package_cost()
    return config


def _no_unknown_package_cost():
    """Stop this package without changing the older ledger's unknown-cost policy."""
    from tests.helpers import phase_b_model_evaluation as models
    owners = []
    for variant in MODEL_SLOTS:
        ready = models._slot(variant, 1, "development", LABEL) / "ready.json"
        if ready.exists():
            owners.append((Store(models.STORE_ROOT), reference._json(ready)["run_id"]))
    for path in ROOT.glob("*-input.json"):
        owners.append((Store(ROOT / "inputs"), reference._json(path)["run_id"]))
    for path in (ROOT / "science-slots").glob("*/metadata.json"):
        owners.append((Store(ROOT / "science"), reference._json(path)["run_id"]))
    for store, run_id in owners:
        run = store.load_run(run_id)
        if (run.state == "unknown" or any(r.get("status") in {"reserved", "unknown"} for r in run.model_records)
                or any(a.state in {"prepared", "running", "unknown"} for a in run.attempts)
                or any(c.state in {"reserved", "unknown", "failed"} for c in run.calls
                       if c.tool in {"structure.resolve", "structure.prepare"})):
            raise reference.ReferenceBlocked("this package has unknown cost/process or a failed input slot; stop and reconcile")


def _require_model_passes(variants):
    from tests.helpers import phase_b_model_evaluation as models
    for variant in variants:
        # Regrade the exact saved trajectory; never trust a free-standing pass flag.
        grade = models.regrade(variant, 1, category="development", freeze_label=LABEL)
        if grade.get("status") != "passed":
            raise reference.ReferenceBlocked(f"independent real-model gate not passed: {variant}")


def model_slot(variant, *, execute=False, live=False):
    if variant not in MODEL_SLOTS:
        raise ValueError("variant outside this fixed package")
    _execution_gate(execute=execute, live=live)
    _require_model_passes(MODEL_SLOTS[:MODEL_SLOTS.index(variant)])
    from tests.helpers import phase_b_model_evaluation as models
    return models.evaluate(variant, 1, category="development", freeze_label=LABEL,
                           model_profile="disabled", allow_live=True)


def _input_slot(system):
    if system not in {"water", "methane"}:
        raise ValueError("unknown fixed system")
    store = Store(ROOT / "inputs")
    metadata_path = ROOT / f"{system}-input.json"
    if metadata_path.exists():
        return store, store.load_run(reference._json(metadata_path)["run_id"])
    reservation = ROOT / f"{system}-input-reserved.json"
    if reservation.exists():
        raise reference.ReferenceBlocked("input preparation interrupted; reconcile existing Run")
    _save(reservation, {"system": system, "scope": LABEL})
    quote = f"Prepare the initial geometry of {system}; neutral singlet RHF/STO-3G."
    evidence = {"message_id": "initial_message", "text_basis": quote}
    identity = {"canonical_names": [system], "text_evidence": evidence}
    request = Request(original_text=quote, messages=[{"id": "initial_message", "text": quote}],
        systems=[SystemInput(id=system, identity=identity, geometry_source="prepare")],
        goals=[Goal(id="science_pending", port="energy", system_ids=[system], identity=identity,
                    text_evidence=evidence, minimum_check_version="orca-hf-2")])
    permission = PermissionSnapshot(allowed_tools=["structure.resolve", "structure.prepare"],
        artifact_writes=True, external_identity_queries=True, geometry_preparation=True,
        scientific_execution=False, model_execution=False)
    run = store.create_run(request, None, permission, BudgetLimits(orca_starts=0, extra_orca_starts=0,
        model_calls=0, identity_queries=1, structure_preparations=1, attempts_per_step=1),
        science_baseline_policy="first_science_plan")
    steps = [Step(id="resolve", logical_id="resolve", tool="structure.resolve", system_id=system,
                  parameters={"system_id": system}),
             Step(id="prepare", logical_id="prepare", tool="structure.prepare", system_id=system,
                  parameters={"system_id": system, "charge": 0, "multiplicity": 1}, depends_on=["resolve"],
                  inputs={"identity": EvidenceRef(producer_step_id="resolve", port="resolved_identity",
                                                  rule_version="structure-identity-1")})]
    plan = Plan(request_id=request.id, steps=steps, goal_map={"science_pending": OutputBinding(
        port="energy", gap="Input acquisition only; scientific work remains required")})
    run = store.commit_revision(run, plan, decision_id="input_only", basis={"request_version": 1,
        "plan_version": None, "permission_version": 1, "control_generation": 0})
    _save(metadata_path, {"system": system, "run_id": run.id, "data_root": str(store.root)})
    return store, run


def _input_result(system, name):
    if not (ROOT / f"{system}-input.json").is_file():
        raise reference.ReferenceBlocked("required input slot has not been executed")
    store, run = _input_slot(system)
    path = ROOT / f"{system}-{name}.json"
    record = reference._json(path)
    result = store.load_result(run.id, record["result_id"])
    if (result.run_id != run.id or result.operation_status != "completed"
            or name not in result.qualified_outputs):
        raise reference.ReferenceBlocked("input slot lacks its checked immutable output")
    if sha256_file(store.path(f"runs/{run.id}/results/{result.id}.json")) != record["result_sha256"]:
        raise reference.ReferenceBlocked("input Result changed")
    artifact = store.load_artifact(result.qualified_outputs[name].artifact_id)
    store.artifact_path(artifact.id)
    return store, run, result, artifact


def input_stage(system, stage, *, execute=False, live=False):
    _execution_gate(execute=execute, live=live)
    _require_model_passes(MODEL_SLOTS)
    from orca_agent.tools.dispatch import execute_call
    name = "resolved_identity" if stage == "resolve" else "prepared_geometry"
    record_path = ROOT / f"{system}-{name}.json"
    if record_path.exists():
        _input_result(system, name)
        if stage == "prepare":
            _freeze_prepared(system)
        return reference._json(record_path)
    if stage == "prepare":
        for expected in ("water", "methane"):
            _input_result(expected, "resolved_identity")
    store, run = _input_slot(system)
    plan = store.load_plan(run)
    step = next(s for s in plan.steps if s.id == stage)
    if any(call.tool == step.tool for call in run.calls):
        raise reference.ReferenceBlocked("existing input Call needs reconciliation; no repeat request/generation")
    results = {} if stage == "resolve" else {"resolve": _input_result(system, "resolved_identity")[2]}
    result = execute_call(store, run, step.tool, step.parameters.model_dump(), step=step, results=results)
    record = {"run_id": run.id, "result_id": result.id,
              "result_sha256": sha256_file(store.path(f"runs/{run.id}/results/{result.id}.json"))}
    _save(record_path, record)
    _input_result(system, name)
    if stage == "prepare":
        _freeze_prepared(system)
    return record


def _freeze_prepared(system):
    store, _, result, artifact = _input_result(system, "prepared_geometry")
    target = ROOT / "frozen-inputs" / system / "geometry.xyz"
    content = store.artifact_path(artifact.id).read_bytes()
    if target.exists():
        if target.read_bytes() != content:
            raise reference.ReferenceBlocked("frozen prepared initial geometry changed")
    else:
        atomic_write(target, content, immutable=True)
    path = target.with_suffix(".provenance.json")
    record = {"run_id": result.run_id, "result_id": result.id, "artifact_id": artifact.id,
              "sha256": artifact.sha256, "role": "explicit immutable prepared initial geometry; not optimized"}
    if path.exists():
        if reference._json(path) != record:
            raise reference.ReferenceBlocked("prepared provenance changed")
    else:
        _save(path, record)


def _prepared(system):
    store, _, _, artifact = _input_result(system, "prepared_geometry")
    path = ROOT / "frozen-inputs" / system / "geometry.xyz"
    if sha256_file(path) != artifact.sha256 or path.read_bytes() != store.artifact_path(artifact.id).read_bytes():
        raise reference.ReferenceBlocked("frozen prepared initial geometry changed")
    return path


def verify_scientific_reference(receipt, geometry_sha256):
    """A verified negative reference is not an energy baseline for this package."""
    receipt = receipt or {}
    independent = receipt.get("independent_output", {})
    energy = independent.get("energy_eh")
    if (receipt.get("reference_verified") is not True
            or receipt.get("execution_uncertain") is not False
            or receipt.get("execution", {}).get("state") != "completed"
            or independent.get("status") != "converged"
            or type(energy) not in (int, float) or not math.isfinite(energy)):
        raise reference.ReferenceBlocked("independent positive scientific reference is not verified")
    if receipt.get("sources", {}).get("geometry_sha256") != geometry_sha256:
        raise reference.ReferenceBlocked("reference does not target this prepared geometry")
    return energy


def methane_reference(*, execute=False, live=False):
    config = _execution_gate(execute=execute, live=live)
    _require_model_passes(MODEL_SLOTS)
    _prepared("water")
    geometry = _prepared("methane")
    path = geometry.parent / "reference.inp"
    expected = reference.reference_input(100).encode()
    if path.exists():
        if path.read_bytes() != expected:
            raise reference.ReferenceBlocked("prewritten independent reference input changed")
    else:
        atomic_write(path, expected, immutable=True)
    receipt = reference.execute_reference(REFERENCE_ID, "reference", geometry, path, 100,
        atom_mapping=["C", "H", "H", "H", "H"], config=config)
    verify_scientific_reference(receipt, sha256_file(geometry))
    return receipt


def science_slot(system, repetition, *, execute=False, live_model=False, live_orca=False):
    config = _execution_gate(execute=execute, live=live_model and live_orca)
    if system not in {"water", "methane"} or type(repetition) is not int or repetition not in (1, 2, 3):
        raise ValueError("unknown science slot")
    _require_model_passes(MODEL_SLOTS)
    slots = [(name, number) for name in ("water", "methane") for number in (1, 2, 3)]
    for previous_system, previous_rep in slots[:slots.index((system, repetition))]:
        if grade_science(previous_system, previous_rep).get("status") != "passed":
            raise reference.ReferenceBlocked("previous scientific/model review gate has not passed")
    receipt = reference.BatchLedger().read(REFERENCE_ID)["receipt"]
    verify_scientific_reference(receipt, sha256_file(_prepared("methane")))
    if sha256_file(WATER_REFERENCE) != WATER_REFERENCE_SHA256:
        raise reference.ReferenceBlocked("independent water reference changed")
    from orca_agent.natural import initialize_bundle
    from orca_agent.report import build_report
    from orca_agent.runner import execute as run_agent
    store = Store(ROOT / "science")
    config = config.model_copy(update={"data_root": store.root})
    path = ROOT / "science-slots" / f"{system}-{repetition}"
    metadata_path = path / "metadata.json"
    if metadata_path.exists():
        metadata = reference._json(metadata_path)
        run = store.load_run(metadata["run_id"])
        return {"run_id": run.id, "state": run.state, "existing_slot": True,
                "message": "no automatic rerun/resume; inspect existing evidence"}
    reservation = path / "reservation.json"
    if reservation.exists():
        raise reference.ReferenceBlocked("science preparation interrupted; reconcile existing slot")
    _save(reservation, {"system": system, "repetition": repetition, "candidate_sha256": sha256_file(ROOT / "candidate.json")})
    atomic_write(path / "geometry.xyz", _prepared(system).read_bytes(), immutable=True)
    text = ("对登记的水分子初始几何做气相 RHF/STO-3G 中性单重态无约束优化，交付严格收敛结构及优化后的电子能和来源。"
            if system == "water" else
            "对登记的甲烷准备结构做气相 RHF/STO-3G 中性单重态固定几何单点，交付电子能及来源；不得称为优化结构。")
    bundle = {"text": text, "geometries": [{"id": system, "file": "geometry.xyz"}],
        "scientific_execution": True, "allowed_tools": ["orca.opt" if system == "water" else "orca.sp"],
        "conditions": {"explain_results": True}, "budget": {"orca_starts": 1, "extra_orca_starts": 0,
            "attempts_per_step": 1, "model_calls": 8, "model_tokens": 48000,
            "identity_queries": 0, "structure_preparations": 0, "transport_retries": 0}}
    _save(path / "user-input.json", bundle)
    run = initialize_bundle(store, config, path / "user-input.json")
    run.batch_category = "development"
    store.save_run(run)
    _save(metadata_path, {"run_id": run.id, "data_root": str(store.root), "system": system,
        "repetition": repetition, "geometry_sha256": sha256_file(path / "geometry.xyz"),
        "input_provenance": str(_prepared(system).with_suffix(".provenance.json")),
        "candidate_sha256": sha256_file(ROOT / "candidate.json"), "model_profile": "disabled"})
    run = run_agent(store, config, run.id, batch=budget.AcceptanceBudget(store))
    report = build_report(store, run)
    _save(path / "report.json", report)
    return {"run_id": run.id, "state": run.state, "report": str(path / "report.json"),
            "independent_science_and_model_review": "required; production goal success alone is not acceptance"}


def _distances(path, mapping):
    rows = path.read_text(encoding="utf-8").splitlines()
    atoms = [row.split() for row in rows[2:] if row.strip()]
    if int(rows[0]) != len(mapping) or [row[0] for row in atoms] != mapping:
        raise reference.ReferenceBlocked("independent XYZ atom order differs")
    points = [[float(value) for value in row[1:]] for row in atoms]
    if any(len(point) != 3 or not all(math.isfinite(v) for v in point) for point in points):
        raise reference.ReferenceBlocked("independent XYZ coordinates invalid")
    return [math.dist(points[i], points[j]) for i in range(len(points)) for j in range(i)]


def grade_science(system, repetition, *, review_path=None):
    """Read-only independent numerical checks plus quoted real-response review."""
    from tests.helpers import phase_b_grade_joint as joint
    from tests.helpers import phase_b_model_cases as cases
    from tests.helpers.phase_b_grading import model_response_evidence
    if system not in {"water", "methane"} or type(repetition) is not int or repetition not in (1, 2, 3):
        raise ValueError("unknown science review slot")
    path = ROOT / "science-slots" / f"{system}-{repetition}"
    metadata = reference._json(path / "metadata.json")
    store = Store(ROOT / "science")
    run = store.load_run(metadata["run_id"])
    if metadata["candidate_sha256"] != sha256_file(ROOT / "candidate.json"):
        raise reference.ReferenceBlocked("science slot targets another candidate")
    text = json.dumps(cases._actions(store, run), ensure_ascii=False, sort_keys=True)
    digest = hashlib.sha256(text.encode()).hexdigest()
    reviews = ("quantity", "unit", "conditions", "source", "limits", "next_action", "proposal_facts", "semantics")
    template = {"run_id": run.id, "model_text_sha256": digest,
                "review": {key: {"passed": None, "quote": "", "rationale": ""} for key in reviews}}
    template_path = path / "review-template.json"
    if not template_path.exists():
        _save(template_path, template)
    review_file = Path(review_path) if review_path else path / "review.json"
    review = reference._json(review_file) if review_file.exists() else {}
    if review and (review.get("run_id") != run.id or review.get("model_text_sha256") != digest):
        raise reference.ReferenceBlocked("independent review is bound to different actual model text")
    reviewed = {key: cases._review_entry(review.get("review", {}).get(key), text) for key in reviews}
    facts = {"single_attempt": len(run.attempts) == 1,
        "goal_and_delivery": bool(run.goal_status) and all(v == "satisfied" for v in run.goal_status.values())
                             and run.delivery_status == "complete",
        "real_model": model_response_evidence(store, run).get("present") is True,
        "real_model_final": model_response_evidence(store, run).get("accepted_final_responses", 0) > 0,
        "single_orca": run.usage.orca_starts_actual == 1 and run.usage.postprocess_starts == 0}
    if len(run.attempts) == 1:
        evidence = joint._attempt_evidence(store, run, run.attempts[0])
        result, raw = evidence["result"], evidence["raw"]
        tool = "orca.opt" if system == "water" else "orca.sp"
        energy = result.qualified_outputs.get("energy")
        facts.update(input_profile=joint._input_profile(evidence, tool), resources=joint._resources(evidence),
            exact_prepared_input=evidence["geometry_sha256"] == sha256_file(_prepared(system)),
            independent_scf=raw["status"] == "converged",
            energy_qualified=joint._qualified(result, "energy", joint.ENERGY_CHECKS))
        if system == "water":
            if sha256_file(WATER_REFERENCE) != WATER_REFERENCE_SHA256:
                raise reference.ReferenceBlocked("water reference changed")
            expected = reference._json(WATER_REFERENCE)
            # Use only the independent reference Run's raw artifacts, never the
            # historical production comparison values in the same review file.
            files = {Path(item["path"]).name: item for item in expected["evidence_files"]
                     if expected["run_id"] in Path(item["path"]).parts}
            for item in files.values():
                if sha256_file(Path(item["path"])) != item["sha256"]:
                    raise reference.ReferenceBlocked("independent water evidence changed")
            ref_raw = reference.independent_output(Path(files["stdout.out"]["path"]))
            expected_energy = ref_raw["energy_eh"]
            output = result.qualified_outputs.get("optimized_geometry")
            facts["strict_final_stage"] = bool(joint._qualified(result, "optimized_geometry",
                joint.GEOMETRY_CHECKS | {"optimization_stage_binding"}) and output and any(
                check.name == "optimization_stage_binding" and check.status == "passed"
                and check.rule_version == "orca-hf-2"
                and check.source.get("rule_version") == "optimization-final-stage-1"
                for check in output.checks) and joint._strict_opt(evidence["files"]["stdout.out"]))
            facts["reference_geometry"] = bool(output and max(abs(a - b) for a, b in zip(
                _distances(store.artifact_path(output.artifact_id), ["O", "H", "H"]),
                _distances(Path(files["job.xyz"]["path"]), ["O", "H", "H"]), strict=True)) <= 1e-5)
        else:
            receipt = reference.BatchLedger().read(REFERENCE_ID)["receipt"]
            expected_energy = verify_scientific_reference(receipt, sha256_file(_prepared(system)))
            facts["reference_exact_geometry"] = True
            facts["not_optimized"] = "optimized_geometry" not in result.qualified_outputs
        facts["independent_energy"] = bool(energy and raw.get("energy_eh") is not None
            and abs(raw["energy_eh"] - expected_energy) <= 1e-7
            and abs(energy.value - raw["energy_eh"]) <= 1e-10)
    passed = all(facts.values()) and all(v["status"] == "passed" for v in reviewed.values())
    report = {"status": "passed" if passed else "not_verified" if all(facts.values()) else "failed",
        "run_id": run.id, "facts": facts, "model_review": reviewed, "model_text_sha256": digest,
        "limitations": scope()["limitations"]}
    if review_path and review:
        saved = path / "review.json"
        if saved.exists() and reference._json(saved) != review:
            raise reference.ReferenceBlocked("existing independent review cannot be silently replaced")
        if not saved.exists():
            _save(saved, review)
    reference._save(path / "grade.json", report)
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", nargs="?", default="proposal",
                        choices=("proposal", "apply", "freeze", "model", "resolve", "prepare", "reference", "science", "grade"))
    parser.add_argument("--variant", choices=MODEL_SLOTS)
    parser.add_argument("--system", choices=("water", "methane"))
    parser.add_argument("--repetition", type=int, choices=(1, 2, 3))
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--review", type=Path)
    for name in ("model", "orca", "network", "opi"):
        parser.add_argument(f"--live-{name}", action="store_true")
    args = parser.parse_args(argv)
    if args.operation == "proposal":
        result = {"status": "proposal_only", "approval_pinned": bool(reference.BOUNDED_APPROVAL_SHA256), "scope": scope()}
    elif args.operation == "apply":
        result = apply_limits(execute=args.execute)
    elif args.operation == "freeze":
        result = freeze_candidate()
    elif args.operation == "grade":
        result = grade_science(args.system, args.repetition, review_path=args.review)
    else:
        ROOT.mkdir(parents=True, exist_ok=True)
        with FileLock(str(ROOT / "operator.lock"), timeout=10):
            if args.operation == "model":
                result = model_slot(args.variant, execute=args.execute, live=args.live_model)
            elif args.operation in {"resolve", "prepare"}:
                result = input_stage(args.system, args.operation, execute=args.execute,
                                     live=args.live_network if args.operation == "resolve" else args.live_opi)
            elif args.operation == "reference":
                result = methane_reference(execute=args.execute, live=args.live_orca)
            else:
                result = science_slot(args.system, args.repetition, execute=args.execute,
                                      live_model=args.live_model, live_orca=args.live_orca)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
