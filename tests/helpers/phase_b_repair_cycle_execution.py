"""Finite acceptance trajectories using the existing product and shared ledger.

Only the explicit operator functions execute. Import, manifests and review are
offline. This module does not choose model actions or invent scientific Goals.
"""

import hashlib
import json
from pathlib import Path

from filelock import FileLock

from orca_agent.models import PermissionSnapshot
from orca_agent.store import Store, atomic_write, sha256_file
from tests.helpers import phase_b_budget as budget
from tests.helpers import phase_b_repair_cycle as cycle

reference = budget.reference
SYSTEMS = ("water", "methane")
SCIENTIFIC_DEPENDENCIES = sorted(cycle.DEPENDENCIES - {"recovery"})


class ReferencePreparationRequired(BaseException):
    """Acceptance-only checkpoint before product scientific reservation."""


def _row(http=0, tokens=0, *, starts=0, category="development", refs=0, inputs=0, dependencies=None):
    declared = dict.fromkeys(cycle.DIMENSIONS, 0)
    declared.update(http_requests=http, tokens=tokens, reference=refs,
                    identity_queries=inputs, structure_preparations=inputs)
    declared[category] = starts
    return {"declared": declared, "dependencies": dependencies or SCIENTIFIC_DEPENDENCIES}


def development_manifest(number):
    """Whole development candidate; allocations remain shared across candidates."""
    candidate_number = number
    manifest = cycle.model_manifest(number)
    slots = manifest["slots"]
    for system in SYSTEMS:
        slots[f"layered/{system}-input"] = _row(inputs=1)
        for repetition in (1, 2, 3):
            slots[f"layered/{system}-{repetition}"] = _row(8, 48000, starts=1)
            slots[f"e2e-development/{system}-{repetition}"] = _row(8, 48000, starts=1, inputs=1)
            slots[f"e2e-development/{system}-{repetition}-reference"] = _row(refs=1)
    slots["layered/methane-reference"] = _row(refs=1)
    for case, starts in {"sampling_left": 4, "sampling_right": 4, "sampling_stop": 3,
                         "repair_success": 2, "repair_exhaustion": 2, "methane_opt_control": 1}.items():
        slots[f"legacy-c/{case}"] = _row(8, 48000, starts=starts)
    slots["conditional-d/sampling"] = _row(8, 48000, starts=4)
    slots["conditional-d/repair"] = _row(8, 48000, starts=2)
    for number in range(1, 9):
        slots[f"targeted-repair/repair-{number}"] = _row(6, 48000, starts=1, inputs=1 if number <= 2 else 0)
    for number in (1, 2):
        slots[f"targeted-repair/reference-{number}"] = _row(refs=1)
    cycle._validate_manifest("development", candidate_number, manifest)
    return manifest


def formal_manifest(number):
    """Recompute the full current matrix, including shared joint coverage once.

    This reports actual maxima; cycle._validate_manifest separately rejects any
    purpose overrun. It never squeezes new tests into an old approved ceiling.
    """
    from tests.helpers import phase_b_model_cases as cases
    cycle.candidate_label("formal", number)
    coverage_path = reference.PROJECT / "docs/acceptance/phase-b/coverage-repair-cycle-20261008.json"
    coverage = reference._json(coverage_path)
    slots, bindings = {}, {}
    model_variants = set(cases.evaluation_variant_ids())
    found_models = set()
    starts = {"water_sp": 1, "sampling_left": 4, "sampling_right": 4, "sampling_stop": 3,
              "repair_success": 2, "repair_exhaustion": 2, "methane_opt_control": 1}
    for entry in coverage["entries"]:
        if entry.get("mapping_status", "mapped") != "mapped" or entry.get("required_repetitions", 3) != 3:
            cycle._fail("formal coverage has incomplete executable mappings")
        if {s["repetition"] for s in entry["formal_slots"]} != {1, 2, 3} or len(entry["formal_slots"]) != 3:
            cycle._fail("formal manifest must retain three distinct slots per variant")
        for actual in entry["formal_slots"]:
            variant, rep = entry["variant_id"], actual["repetition"]
            requirement = entry["evidence_requirement"]
            if requirement == "real_model_with_frozen_evidence":
                if variant not in model_variants:
                    cycle._fail("formal model coverage lacks its actual evaluator")
                spec = cases.variant_spec(variant)
                found_models.add(variant)
                identity = "model-" + variant.replace("/", "__") + f"-{rep}"
                row = _row(spec["budget"]["model_http_requests"], spec["budget"]["model_tokens_total"], category="formal")
            elif requirement == "joint_real_model_orca":
                case = actual.get("joint_case")
                if case not in starts:
                    cycle._fail("formal joint coverage has unknown case")
                identity = f"joint-{case}-{rep}"
                row = _row(8, 48000, starts=starts[case], category="formal")
            elif requirement == "offline_fault_injection":
                if not actual.get("pytest_nodeids"):
                    cycle._fail("formal offline slot has no executable test")
                identity = "offline-" + variant.replace("/", "__") + f"-{rep}"
                row = _row(category="formal")
            else:
                cycle._fail("unknown formal evidence type cannot be relabeled to save budget")
            key = f"formal-{number}/{identity}"
            if key in slots and slots[key] != row:
                cycle._fail("shared formal trajectory has conflicting limits")
            slots[key] = row
            bindings[f"{variant}/{rep}"] = {"slot": key, "requirement": requirement,
                "pytest_nodeids": actual["pytest_nodeids"]}
    if found_models != model_variants:
        cycle._fail("formal coverage omits current real model variants")
    for system in SYSTEMS:
        for repetition in (1, 2, 3):
            slots[f"formal-e2e-{number}/{system}-{repetition}"] = _row(8, 48000, starts=1, inputs=1, category="formal")
            slots[f"formal-e2e-{number}/{system}-{repetition}-reference"] = _row(refs=1, category="formal")
    return {"schema_version": 1, "coverage_sha256": sha256_file(coverage_path),
            "coverage_bindings": bindings, "slots": slots}


def freeze_formal_candidate(number, *, repair_evidence=None):
    """One local adapter for the existing formal freeze and cycle source receipt."""
    from tests.helpers.phase_b_formal_ops import freeze
    candidate = cycle.candidate_label("formal", number)
    manifest = cycle.formal_freeze_requirements(candidate, repair_evidence=repair_evidence)
    freeze(candidate, with_science=True, model_profile="disabled", cycle_repair_evidence=repair_evidence)
    return cycle.freeze_candidate("formal", number, manifest=manifest, repair_evidence=repair_evidence)


def manifest_budget(manifest):
    return {allocation: {dimension: sum(row["declared"][dimension] for key, row in manifest["slots"].items()
                         if key.startswith(allocation + "/")) for dimension in cycle.DIMENSIONS}
            for allocation in dict.fromkeys(key.split("/", 1)[0] for key in manifest["slots"])}


def _root(candidate):
    cycle.parse_candidate(candidate)
    return reference.BATCH_ROOT / cycle.CYCLE_ID / "trajectories" / candidate


def _open(candidate, allocation, slot_id, *, execute, live):
    if not execute or not live:
        cycle._fail("cycle trajectory requires explicit execute and live switches")
    book = cycle._book()
    with book.ledger._lock():
        ledger = book._snapshot_unlocked()
        state = cycle._state(ledger)
        from tests.helpers.phase_b_development_amendment import assert_open
        assert_open(ledger, candidate)
        cycle._no_unknown(ledger, state)
        frozen = cycle._candidate(state, candidate)
        row = frozen["manifest"]["slots"].get(f"{allocation}/{slot_id}")
        if row is None:
            cycle._fail("trajectory is absent from the frozen candidate manifest")
        cycle._dependencies(state, candidate, row["dependencies"], scientific=True)
        return frozen, row


def _config(candidate, store):
    from tests.helpers.phase_b_freeze import evaluation_config
    config = evaluation_config(science=True, model_profile="disabled")
    return config.model_copy(update={"data_root": store.root})


def _bind(candidate, allocation, slot_id, row, store, run):
    return cycle.bind_slot(candidate, allocation, slot_id, run=run, store=store, **row)


def _save(path, value):
    reference._save(path, value, immutable=True)


def _reserved(path, value):
    if path.exists():
        cycle._fail("trajectory already reserved; inspect the same Run, never repeat initialization")
    _save(path, value)


def _report(store, run, directory):
    from orca_agent.report import build_report
    report = build_report(store, run)
    _save(directory / "report.json", report)
    return {"run_id": run.id, "state": run.state, "report": str(directory / "report.json"),
            "acceptance": "requires independent actual-response and scientific review"}


def _e2e_profile(config, *, category):
    from orca_agent.config import TextProfile
    from orca_agent.natural import agent_budget
    return config.model_copy(update={"text": TextProfile(enabled=True,
        permission=PermissionSnapshot(model_execution=True, scientific_execution=True,
            artifact_writes=True, external_identity_queries=True, geometry_preparation=True,
            allowed_tools=["structure.resolve", "structure.prepare", "orca.opt", "orca.sp"]),
        budget=agent_budget(orca_starts=1, extra_orca_starts=0, attempts_per_step=1,
                            identity_queries=1, structure_preparations=1, transport_retries=0))})


def _pending_geometry(store, run):
    """Read the persisted model-selected science Step after production validation."""
    from orca_agent import runner
    from orca_agent.applicability import validate_geometry_consumption
    from orca_agent.tools.registry import get_tool
    pending = [d for d in run.decisions if d.get("action") == "call_tool" and d["id"] not in run.applied_decisions]
    if len(pending) != 1 or run.attempts or run.usage.orca_starts_reserved:
        cycle._fail("E2E checkpoint must precede the sole first science reservation")
    step_id = pending[0].get("parameters", {}).get("step_id")
    plan = store.load_plan(run)
    steps = [s for s in plan.steps if s.id == step_id and s.tool in {"orca.opt", "orca.sp"}]
    if len(steps) != 1:
        cycle._fail("checkpoint has no unique model-selected science Step")
    step = steps[0]
    geometry = step.geometry
    if geometry.artifact_id:
        artifact_id = geometry.artifact_id
    else:
        result = runner._step_results(store, run).get(geometry.producer_step_id)
        output = result.qualified_outputs.get(geometry.port) if result else None
        if (not output or not output.artifact_id or any(c.rule_version != get_tool(step.tool).required_input_checks.get(geometry.port)
                                                       for c in output.checks)):
            cycle._fail("E2E geometry no longer qualifies under production consumption")
        artifact_id = output.artifact_id
    validate_geometry_consumption(store, run, step, artifact_id)
    artifact = store.load_artifact(artifact_id)
    path = store.artifact_path(artifact.id)
    prepared = [store.load_result(run.id, call.result_id) for call in run.calls
                if call.tool == "structure.prepare" and call.state == "completed" and call.result_id]
    output = prepared[0].qualified_outputs.get("prepared_geometry") if len(prepared) == 1 else None
    if not output or output.artifact_id != artifact.id:
        cycle._fail("E2E science must consume this same Run's checked preparation")
    return step, artifact, path


def _reference(candidate, allocation, slot_id, geometry, *, job_type, config):
    frozen, row = _open(candidate, allocation, slot_id, execute=True, live=True)
    directory = _root(candidate) / allocation / slot_id
    directory.mkdir(parents=True, exist_ok=True)
    input_path = directory / "reference.inp"
    atomic_write(input_path, reference.reference_input(100, job_type=job_type).encode(), immutable=True)
    mapping = [row.split()[0] for row in geometry.read_text().splitlines()[2:] if row.strip()]
    sources = reference.reviewed_sources(geometry, input_path, 100, job_type=job_type, atom_mapping=mapping)
    identity = "cycle-reference-" + cycle._digest([candidate, allocation, slot_id])[:24]
    slot = cycle.bind_slot(candidate, allocation, slot_id, reference_id=identity, reference_sources=sources, **row)
    _save(directory / "metadata.json", {"candidate_sha256": frozen["receipt"]["sha256"], "reference_id": identity,
                                         "slot_receipt_sha256": slot["receipt"]["sha256"]})
    with cycle.activity(slot, seconds=120):
        receipt = reference.execute_reference(identity, "reference", geometry, input_path, 100,
                                              job_type=job_type, atom_mapping=mapping, config=config)
    from tests.helpers.phase_b_bounded_package import verify_scientific_reference
    passed = False
    try:
        verify_scientific_reference(receipt, sha256_file(geometry))
        passed = True
    except reference.ReferenceBlocked:
        pass
    path = reference.BatchLedger().receipt_path(identity)
    cycle.record_outcome(slot, status="passed" if passed else "failed",
        failure_kind=None if passed else "unknown_process" if receipt.get("execution_uncertain") else "ordinary",
        affected_dependencies=[] if passed else ["science"],
        evidence=[{"path": str(path), "sha256": sha256_file(path)}])
    if not passed:
        cycle._fail("independent reference failed; related product science remains unlaunched")
    return identity, receipt


def e2e_slot(candidate, system, repetition, *, execute=False, live_model=False, live_orca=False):
    """Fresh pure text → model-selected input tools → independent reference → same Run."""
    from orca_agent.natural import initialize_text
    from orca_agent.runner import execute as run_agent
    kind, number = cycle.parse_candidate(candidate)
    if system not in SYSTEMS or type(repetition) is not int or repetition not in (1, 2, 3):
        cycle._fail("E2E identity outside fixed six independent Runs")
    allocation = "e2e-development" if kind == "development" else f"formal-e2e-{number}"
    slot_id = f"{system}-{repetition}"
    frozen, row = _open(candidate, allocation, slot_id, execute=execute, live=live_model and live_orca)
    directory = _root(candidate) / allocation / slot_id
    store = Store(_root(candidate) / "agent")
    config = _e2e_profile(_config(candidate, store), category=kind)
    text = ("没有XYZ。请取得水分子的初始几何，以气相 RHF/STO-3G 中性单重态做无约束优化，"
            "交付严格收敛结构、优化后的电子能及来源。" if system == "water" else
            "没有XYZ。请取得甲烷初始几何，以气相 RHF/STO-3G 中性单重态在所准备的固定几何上做单点，"
            "交付电子能及来源，不得称为优化结构。")
    with FileLock(str(_root(candidate) / "operator.lock"), timeout=10):
        _reserved(directory / "reservation.json", {"candidate_sha256": frozen["receipt"]["sha256"],
                                                    "text": text, "system": system, "repetition": repetition})
        run = initialize_text(store, config, text)
        run.batch_category = kind
        store.save_run(run)
        slot = _bind(candidate, allocation, slot_id, row, store, run)
        metadata = {"run_id": run.id, "data_root": str(store.root), "candidate_sha256": frozen["receipt"]["sha256"],
                    "system": system, "repetition": repetition, "input_form": "pure_text", "text": text,
                    "slot_receipt_sha256": slot["receipt"]["sha256"], "terminal_contract_version": "terminal-delivery-1"}
        _save(directory / "metadata.json", metadata)
        paused = False
        def before_reference(stage):
            if stage == "before_science_reservation":
                raise ReferencePreparationRequired()
        with cycle.activity(slot, seconds=run.budget.run_seconds, segment="before_reference"):
            try:
                run = run_agent(store, config, run.id, batch=budget.AcceptanceBudget(store), fault=before_reference)
            except ReferencePreparationRequired:
                paused = True
        run = store.load_run(run.id)
        if not paused:
            return _report(store, run, directory)
        step, artifact, geometry = _pending_geometry(store, run)
        expected_tool = "orca.opt" if system == "water" else "orca.sp"
        if step.tool != expected_tool:
            cycle._fail("actual E2E model selected a scientific job outside the user request")
        frozen_geometry = directory / "prepared.xyz"
        atomic_write(frozen_geometry, geometry.read_bytes(), immutable=True)
        checkpoint = {"run_id": run.id, "step_id": step.id, "geometry_artifact_id": artifact.id,
            "geometry_sha256": artifact.sha256, "request_version": run.request_version,
            "plan_version": run.plan_version, "permission_version": run.permission.version,
            "deadline": run.deadline.isoformat() if run.deadline else None}
        _save(directory / "before-science.json", checkpoint)
        identity, _ = _reference(candidate, allocation, slot_id + "-reference", frozen_geometry,
                                   job_type=step.tool.removeprefix("orca."), config=config)
        _save(directory / "reference.json", {"reference_id": identity,
            "receipt_sha256": sha256_file(reference.BatchLedger().receipt_path(identity))})
        def verify_reference(stage):
            if stage != "before_science_reservation":
                return
            current = store.load_run(run.id)
            actual_step, actual_artifact, _ = _pending_geometry(store, current)
            if (actual_step.id != checkpoint["step_id"] or actual_artifact.sha256 != artifact.sha256
                    or current.request_version != checkpoint["request_version"]
                    or current.plan_version != checkpoint["plan_version"]
                    or current.permission.version != checkpoint["permission_version"]
                    or (current.deadline.isoformat() if current.deadline else None) != checkpoint["deadline"]):
                cycle._fail("E2E pending science or original deadline changed across independent reference")
            from tests.helpers.phase_b_bounded_package import verify_scientific_reference
            verify_scientific_reference(reference.BatchLedger().read(identity)["receipt"], artifact.sha256)
        verify_reference("before_science_reservation")
        with cycle.activity(slot, seconds=run.budget.run_seconds, segment="after_reference"):
            run = run_agent(store, config, run.id, resume=True, batch=budget.AcceptanceBudget(store), fault=verify_reference)
        return _report(store, run, directory)


def joint_slot(candidate, case, *, repetition=1, execute=False, live_model=False, live_orca=False):
    """The original C trajectory definitions, without changing their Request."""
    from orca_agent.runner import execute as run_agent
    from tests.helpers.phase_b_joint import prepare_case
    kind, number = cycle.parse_candidate(candidate)
    if type(repetition) is not int or repetition not in ((1,) if kind == "development" else (1, 2, 3)):
        cycle._fail("joint trajectory repetition outside this candidate")
    allocation, slot_id = ("legacy-c", case) if kind == "development" else (f"formal-{number}", f"joint-{case}-{repetition}")
    frozen, row = _open(candidate, allocation, slot_id, execute=execute, live=live_model and live_orca)
    directory = _root(candidate) / allocation / slot_id
    store = Store(_root(candidate) / "agent")
    config = _config(candidate, store)
    with FileLock(str(_root(candidate) / "operator.lock"), timeout=10):
        _reserved(directory / "reservation.json", {"candidate_sha256": frozen["receipt"]["sha256"], "case": case})
        run, metadata = prepare_case(store, config, case, kind)
        slot = _bind(candidate, allocation, slot_id, row, store, run)
        metadata.update(run_id=run.id, data_root=str(store.root), candidate=candidate,
                        candidate_sha256=frozen["receipt"]["sha256"], terminal_contract_version="terminal-delivery-1")
        _save(directory / "metadata.json", metadata)
        with cycle.activity(slot, seconds=run.budget.run_seconds):
            run = run_agent(store, config, run.id, batch=budget.AcceptanceBudget(store))
        return _report(store, run, directory)


def layered_input(candidate, system, stage, *, execute=False, live=False):
    """Original deterministic input layer; explicitly separate from model E2E."""
    from orca_agent.tools.dispatch import execute_call
    from tests.helpers import phase_b_bounded_package as package
    if system not in SYSTEMS or stage not in {"resolve", "prepare"}:
        cycle._fail("unknown layered input slot")
    _, row = _open(candidate, "layered", f"{system}-input", execute=execute, live=live)
    _root(candidate).mkdir(parents=True, exist_ok=True)
    with FileLock(str(_root(candidate) / "operator.lock"), timeout=10):
        if stage == "prepare":
            for name in SYSTEMS:
                package._input_result(name, "resolved_identity", package=candidate)
        store, run = package._input_slot(system, package=candidate)
        if run.batch_category is None:
            run.batch_category = "development"
            store.save_run(run)
        slot = _bind(candidate, "layered", f"{system}-input", row, store, run)
        plan = store.load_plan(run)
        step = next(s for s in plan.steps if s.id == stage)
        if any(call.tool == step.tool for call in run.calls):
            cycle._fail("layered input already attempted; no repeated query/preparation")
        results = {} if stage == "resolve" else {"resolve": package._input_result(system, "resolved_identity", package=candidate)[2]}
        with cycle.activity(slot, seconds=65 if stage == "resolve" else 30, segment=stage):
            result = execute_call(store, run, step.tool, step.parameters.model_dump(), step=step, results=results)
        port = "resolved_identity" if stage == "resolve" else "prepared_geometry"
        path = _root(candidate) / f"{system}-{port}.json"
        _save(path, {"run_id": run.id, "result_id": result.id,
                     "result_sha256": sha256_file(store.path(f"runs/{run.id}/results/{result.id}.json"))})
        passed = result.operation_status == "completed" and port in result.qualified_outputs
        if stage == "prepare" or not passed:
            cycle.record_outcome(slot, status="passed" if passed else "failed", failure_kind=None if passed else "ordinary",
                affected_dependencies=[] if passed else ["structure"],
                evidence=[{"path": str(path), "sha256": sha256_file(path)}])
        package._input_result(system, port, package=candidate)
        if stage == "prepare":
            package._freeze_prepared(system, package=candidate)
        return reference._json(path)


def layered_reference(candidate, *, execute=False, live=False):
    from tests.helpers import phase_b_bounded_package as package
    _open(candidate, "layered", "methane-reference", execute=execute, live=live)
    package._prepared("water", package=candidate)
    geometry = package._prepared("methane", package=candidate)
    return _reference(candidate, "layered", "methane-reference", geometry, job_type="sp",
                       config=_config(candidate, Store(_root(candidate) / "agent")))


def layered_science(candidate, system, repetition, *, execute=False, live_model=False, live_orca=False):
    """Prepared once, then three independent model/science Runs per system."""
    from orca_agent.natural import initialize_bundle
    from orca_agent.runner import execute as run_agent
    from tests.helpers import phase_b_bounded_package as package
    if system not in SYSTEMS or type(repetition) is not int or repetition not in (1, 2, 3):
        cycle._fail("unknown layered science slot")
    slot_id = f"{system}-{repetition}"
    frozen, row = _open(candidate, "layered", slot_id, execute=execute, live=live_model and live_orca)
    geometry = package._prepared(system, package=candidate)
    ref_metadata = reference._json(_root(candidate) / "layered/methane-reference/metadata.json")
    package.verify_scientific_reference(reference.BatchLedger().read(ref_metadata["reference_id"])["receipt"],
                                       sha256_file(package._prepared("methane", package=candidate)))
    if sha256_file(package.WATER_REFERENCE) != package.WATER_REFERENCE_SHA256:
        cycle._fail("original independent water Opt reference changed")
    directory = _root(candidate) / "layered" / slot_id
    store = Store(_root(candidate) / "agent")
    config = _config(candidate, store)
    with FileLock(str(_root(candidate) / "operator.lock"), timeout=10):
        _reserved(directory / "reservation.json", {"candidate_sha256": frozen["receipt"]["sha256"], "system": system})
        atomic_write(directory / "geometry.xyz", geometry.read_bytes(), immutable=True)
        text = ("对登记的水分子初始几何做气相 RHF/STO-3G 中性单重态无约束优化，交付严格收敛结构及优化后的电子能和来源。"
                if system == "water" else "对登记的甲烷准备结构做气相 RHF/STO-3G 中性单重态固定几何单点，交付电子能及来源；不得称为优化结构。")
        bundle = {"text": text, "geometries": [{"id": system, "file": "geometry.xyz"}],
            "scientific_execution": True, "allowed_tools": ["orca.opt" if system == "water" else "orca.sp"],
            "conditions": {"explain_results": True}, "budget": {"orca_starts": 1, "extra_orca_starts": 0,
            "attempts_per_step": 1, "model_calls": 8, "model_tokens": 48000,
            "identity_queries": 0, "structure_preparations": 0, "transport_retries": 0}}
        _save(directory / "user-input.json", bundle)
        run = initialize_bundle(store, config, directory / "user-input.json")
        run.batch_category = "development"
        store.save_run(run)
        slot = _bind(candidate, "layered", slot_id, row, store, run)
        _save(directory / "metadata.json", {"run_id": run.id, "data_root": str(store.root), "system": system,
            "repetition": repetition, "geometry_sha256": sha256_file(geometry),
            "candidate_sha256": frozen["receipt"]["sha256"], "slot_receipt_sha256": slot["receipt"]["sha256"],
            "terminal_contract_version": "terminal-delivery-1"})
        with cycle.activity(slot, seconds=run.budget.run_seconds):
            run = run_agent(store, config, run.id, batch=budget.AcceptanceBudget(store))
        return _report(store, run, directory)


def grade_trajectory(candidate, allocation, slot_id, *, review_path=None):
    """Read actual raw responses, current terminal receipt and independent science."""
    from orca_agent.model_usage import read_model_reply
    from tests.helpers import phase_b_bounded_package as package
    from tests.helpers import phase_b_grade_joint as joint
    from tests.helpers import phase_b_model_cases as cases
    from tests.helpers.phase_b_grading import model_response_evidence, terminal_delivery_evidence
    directory = _root(candidate) / allocation / slot_id
    metadata = reference._json(directory / "metadata.json")
    book = cycle._book()
    state = cycle._state(book.snapshot())
    frozen = state["candidates"].get(candidate)
    if not frozen or metadata.get("candidate_sha256") != frozen["receipt"]["sha256"]:
        cycle._fail("trajectory review targets a different frozen candidate")
    if metadata.get("terminal_contract_version") != "terminal-delivery-1":
        cycle._fail("cycle trajectory cannot use historical terminal grading")
    matches = [s for s in state["slots"].values() if (s["candidate"], s["allocation"], s["slot_id"])
               == (candidate, allocation, slot_id)]
    if len(matches) != 1 or matches[0].get("run_id") != metadata["run_id"]:
        cycle._fail("trajectory has no exact immutable Run slot")
    store = Store(Path(metadata["data_root"]))
    run = store.load_run(metadata["run_id"])
    original = []
    for record in run.model_records:
        reply, _ = read_model_reply(store, run, record)
        original.append({"id": record["id"], "proposal": reply.proposal, "raw_content": reply.raw_content,
                         "error_category": reply.error_category})
    text = json.dumps(original, ensure_ascii=False, sort_keys=True)
    digest = hashlib.sha256(text.encode()).hexdigest()
    axes = (*cases.EXPLANATION_AXES, "proposal_facts", "semantics")
    template = {"run_id": run.id, "candidate_sha256": frozen["receipt"]["sha256"],
        "model_text_sha256": digest,
        "instructions": "Independently review every original response, including rejected attempts; renderer correctness cannot establish prose quality.",
        "review": {axis: {"passed": None, "quote": "", "rationale": ""} for axis in axes}}
    if not (directory / "review-template.json").exists():
        _save(directory / "review-template.json", template)
    review_file = Path(review_path) if review_path else directory / "review.json"
    review = reference._json(review_file) if review_file.exists() else {}
    if review and any(review.get(k) != template[k] for k in ("run_id", "candidate_sha256", "model_text_sha256")):
        cycle._fail("review is not bound to all actual original responses")
    reviewed = {axis: cases._review_entry(review.get("review", {}).get(axis), text) for axis in axes}
    facts = {"real_model": model_response_evidence(store, run).get("present") is True,
             "terminal_contract": terminal_delivery_evidence(store, run)["passed"]}
    try:
        if allocation == "legacy-c" or slot_id.startswith("joint-"):
            graded = joint.grade_joint(store, run, metadata["case"], metadata=metadata)
            facts["independent_joint_trajectory"] = graded["passed"]
            extra = {"joint_grade": graded}
        else:
            from orca_agent.report import build_report
            system = metadata["system"]
            mapping = ["O", "H", "H"] if system == "water" else ["C", "H", "H", "H", "H"]
            facts.update(single_orca=run.usage.orca_starts_actual == 1 and run.usage.orca_starts_reserved == 1,
                         no_postprocess=run.usage.postprocess_starts == 0,
                         user_goal_complete=build_report(store, run)["user_goal_complete"])
            is_e2e = allocation == "e2e-development" or allocation.startswith("formal-e2e-")
            if is_e2e:
                geometry = directory / "prepared.xyz"
                checkpoint = reference._json(directory / "before-science.json") if (directory / "before-science.json").exists() else {}
                facts.update(same_run_input_chain=run.usage.identity_queries == run.usage.structure_preparations == 1,
                    checkpoint_before_science=checkpoint.get("run_id") == run.id and len(run.attempts) == 1
                    and run.attempts[0].geometry_artifact_id == checkpoint.get("geometry_artifact_id"))
            else:
                geometry = package._prepared(system, package=candidate)
            reference_xyz, expected_energy = None, None
            if is_e2e or system == "methane":
                binding_path = directory / "reference.json" if is_e2e else _root(candidate) / "layered/methane-reference/metadata.json"
                if binding_path.exists():
                    binding = reference._json(binding_path)
                    receipt_path = reference.BatchLedger().receipt_path(binding["reference_id"])
                    if is_e2e and sha256_file(receipt_path) != binding["receipt_sha256"]:
                        cycle._fail("pre-science reference receipt changed")
                    receipt = reference.BatchLedger().read(binding["reference_id"])["receipt"]
                    expected_energy = package.verify_scientific_reference(receipt, sha256_file(geometry))
                    if system == "water":
                        reference_xyz = next(Path(item["path"]) for item in receipt["evidence_files"] if Path(item["path"]).name == "job.xyz")
                    if is_e2e:
                        from datetime import datetime
                        facts["reference_before_product_attempt"] = bool(len(run.attempts) == 1 and
                            datetime.fromisoformat(receipt["recorded_at"]) < run.attempts[0].created_at)
            else:
                if sha256_file(package.WATER_REFERENCE) != package.WATER_REFERENCE_SHA256:
                    cycle._fail("independent water reference changed")
                receipt = reference._json(package.WATER_REFERENCE)
                files = {Path(item["path"]).name: item for item in receipt["evidence_files"] if receipt["run_id"] in Path(item["path"]).parts}
                for item in files.values():
                    cycle._evidence({"path": item["path"], "sha256": item["sha256"]})
                reference_xyz = Path(files["job.xyz"]["path"])
                expected_energy = reference.independent_output(Path(files["stdout.out"]["path"]))["energy_eh"]
            facts["positive_reference"] = expected_energy is not None
            if len(run.attempts) == 1:
                actual = joint._attempt_evidence(store, run, run.attempts[0])
                result, raw = actual["result"], actual["raw"]
                energy = result.qualified_outputs.get("energy")
                facts.update(resources=joint._resources(actual), exact_prepared_input=actual["geometry_sha256"] == sha256_file(geometry),
                    input_profile=joint._input_profile(actual, "orca.opt" if system == "water" else "orca.sp"),
                    energy_qualified=joint._qualified(result, "energy", joint.ENERGY_CHECKS),
                    independent_energy=bool(energy and raw["status"] == "converged" and expected_energy is not None
                        and abs(raw["energy_eh"] - expected_energy) <= 1e-7 and abs(energy.value - raw["energy_eh"]) <= 1e-10))
                if system == "water":
                    output = result.qualified_outputs.get("optimized_geometry")
                    opt = reference.independent_optimization_output(actual["files"]["stdout.out"], actual["files"]["job.xyz"], mapping)
                    facts["strict_final_optimization"] = bool(output and opt["status"] == "converged"
                        and joint._qualified(result, "optimized_geometry", joint.GEOMETRY_CHECKS | {"optimization_stage_binding"})
                        and any(c.name == "optimization_stage_binding" and c.source.get("rule_version") == "optimization-final-stage-1" for c in output.checks))
                    facts["reference_terminal_geometry"] = bool(output and reference_xyz and max(abs(a - b) for a, b in zip(
                        package._distances(store.artifact_path(output.artifact_id), mapping), package._distances(reference_xyz, mapping), strict=True)) <= 1e-5)
                else:
                    facts["not_optimized"] = "optimized_geometry" not in result.qualified_outputs
            extra = {}
    except (OSError, ValueError, KeyError, TypeError, StopIteration, RuntimeError) as exc:
        facts["scientific_evidence_available"] = False
        extra = {"evidence_error": type(exc).__name__}
    failed = not all(facts.values()) or any(v["status"] == "failed" for v in reviewed.values())
    passed = not failed and all(v["status"] == "passed" for v in reviewed.values())
    grade = {"status": "failed" if failed else "passed" if passed else "not_verified", "run_id": run.id,
        "facts": facts, "model_review": reviewed, "model_text_sha256": digest, **extra,
        "limitations": "Same ORCA engine; no cross-engine, global-minimum, frequency or statistical reliability claim."}
    if review_path and review and not (directory / "review.json").exists():
        _save(directory / "review.json", review)
    reference._save(directory / "grade.json", grade)
    return grade


def record_trajectory_outcome(candidate, allocation, slot_id, *, review_path=None, failure_kind=None, affected_dependencies=()):
    grade = grade_trajectory(candidate, allocation, slot_id, review_path=review_path)
    if grade["status"] == "not_verified":
        cycle._fail("actual independent trajectory review is still required")
    state = cycle._state(cycle._book().snapshot())
    slot = next(s for s in state["slots"].values() if (s["candidate"], s["allocation"], s["slot_id"]) == (candidate, allocation, slot_id))
    path = _root(candidate) / allocation / slot_id / "grade.json"
    return cycle.record_outcome(slot, status=grade["status"], failure_kind=failure_kind,
        affected_dependencies=affected_dependencies, evidence=[{"path": str(path), "sha256": sha256_file(path)}])
