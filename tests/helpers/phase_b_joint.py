"""Explicitly gated real joint trajectories; never constructs a hand-written Plan."""

import argparse
import json
from pathlib import Path

from orca_agent.models import Goal, PermissionSnapshot, Request, SystemInput, new_id
from orca_agent.natural import agent_budget, initialize_agent, initialize_bundle
from orca_agent.report import build_report, render_report
from orca_agent.runner import execute
from orca_agent.store import Store, atomic_write, sha256_file
from tests.helpers.phase_b_budget import AcceptanceBudget
from tests.helpers.phase_b_freeze import evaluation_config

PROJECT = Path(__file__).resolve().parents[2]
ROOT = PROJECT / "data" / "phase-b"
CASES = ("water_sp", "repair_success", "repair_exhaustion", "sampling_left", "sampling_right",
         "sampling_stop", "methane_opt_control")


def _json(path):
    return json.loads(path.read_text(encoding="utf-8"))


def _write(path, data, *, immutable=False):
    atomic_write(path, (json.dumps(data, indent=2, ensure_ascii=False) + "\n").encode(), immutable=immutable)


def prepare_case(store, config, case, category):
    if case not in CASES or category not in {"development", "formal"}:
        raise ValueError("unknown fixed evaluation case/category")
    if case == "methane_opt_control":
        # Preserve the original independent CH4 Opt expectation, but exercise
        # the raw entry rather than giving the model a pre-normalized Goal.
        source = PROJECT / "tests/fixtures/phase_a/methane_opt/geometry.xyz"
        frozen = _json(PROJECT / "tests/fixtures/phase_b/cases.json")["common"]["fixed_input_files"]["methane_opt"]
        if sha256_file(source) != frozen["sha256"]:
            raise ValueError("frozen initial geometry changed")
        directory = store.path(f"evaluation-inputs/{new_id('input')}")
        directory.mkdir(parents=True)
        atomic_write(directory / "methane.xyz", source.read_bytes(), immutable=True)
        bundle = {"text": "对登记的甲烷初始几何做气相 RHF/STO-3G 中性单重态无约束优化，"
                          "交付通过严格收敛检查的结构，并给出优化后的电子能及其来源。",
                  "geometries": [{"id": "methane", "file": "methane.xyz"}],
                  "scientific_execution": True, "allowed_tools": ["orca.opt"],
                  "conditions": {"explain_results": True},
                  "budget": {"orca_starts": 1, "extra_orca_starts": 0}}
        path = directory / "user-input.json"
        _write(path, bundle, immutable=True)
        run = initialize_bundle(store, config, path)
        run.batch_category = category
        store.save_run(run)
        return run, {"case": case, "category": category, "evidence_type": "joint_real_model_orca",
                     "input_form": "raw_text", "raw_bundle_path": str(path),
                     "raw_bundle_sha256": sha256_file(path),
                     "shared_coverage": ["N-01/raw-entry", "N-02/optimized-energy-relation"]}
    settings = {"method": "HF", "basis": "STO-3G", "charge": 0, "multiplicity": 1}
    permission = PermissionSnapshot(model_execution=True, scientific_execution=True,
                                    allowed_tools=["orca.sp"])
    budget = agent_budget()
    metadata = {"case": case, "category": category, "evidence_type": "joint_real_model_orca"}
    systems = []
    if case.startswith("sampling_"):
        label = case.removeprefix("sampling_")
        manifest = _json(PROJECT / "tests/fixtures/phase_b/sampling-candidates.json")
        window = next(w for w in manifest["windows"] if w["id"] == label)
        context_path = PROJECT / window["model_context_fixture"]
        if sha256_file(context_path) != window["model_context_sha256"]:
            raise ValueError("frozen model input changed")
        context = _json(context_path)
        candidates = []
        for item in window["candidates"]:
            path = PROJECT / item["path"]
            if sha256_file(path) != item["sha256"]:
                raise ValueError("frozen candidate source changed")
            artifact = store.import_artifact(path, "initial_geometry", expected_sha256=item["sha256"])
            candidates.append({"id": item["model_candidate_id"], "artifact_id": artifact.id,
                               "sha256": artifact.sha256, "declared_r_angstrom": item["declared_r_angstrom"],
                               "required_initial": item["required_initial"]})
            systems.append(SystemInput(id=item["model_candidate_id"], geometry_artifact_id=artifact.id,
                                       conditions=settings, atom_mapping=[0, 1, 2]))
        goal = Goal(id="sampling_goal", port="sampling", minimum_check_version="finite-sampling-1",
                    original_text=context["user_message"], conditions={
                        "candidates": candidates, "sampling": {
                            "target_width_angstrom": manifest["target_width_angstrom"],
                            "energy_threshold_eh": context["scan"]["numerical_distinction_threshold_eh"],
                            "fixed_bond_angstrom": manifest["source"]["r02_angstrom"],
                            "fixed_angle_degrees": manifest["source"]["angle_degrees"],
                            **manifest["atom_mapping"]}})
        request = Request(original_text=context["user_message"], systems=systems, **settings,
                          goals=[goal], normalization_status="normalized",
                          conditions={"initial_system_ids": [c["id"] for c in candidates if c["required_initial"]]})
        permission.allowed_tools += ["analysis.finite_sampling"]
        permission.artifact_writes = True
        permission.allow_additional_science = True
        permission.artifact_ids = [s.geometry_artifact_id for s in systems]
        budget = (agent_budget(orca_starts=3, extra_orca_starts=0) if label == "stop"
                  else agent_budget(extra_orca_starts=1))
        metadata.update(candidate_aliases={c["model_candidate_id"]: c["id"] for c in window["candidates"]},
                        reference_window=label)
    else:
        methane = case == "methane_opt_control"
        source = PROJECT / ("tests/fixtures/phase_a/methane_opt/geometry.xyz" if methane
                            else "tests/fixtures/phase_a/water_sp/geometry.xyz")
        frozen = _json(PROJECT / "tests/fixtures/phase_b/cases.json")["common"]["fixed_input_files"][
            "methane_opt" if methane else "water_sp"]
        if sha256_file(source) != frozen["sha256"]:
            raise ValueError("frozen initial geometry changed")
        artifact = store.import_artifact(source, "initial_geometry")
        permission.artifact_ids = [artifact.id]
        text = ("对登记的甲烷初始几何做 RHF/STO-3G 中性单重态无约束优化，交付通过严格收敛检查的结构。"
                if methane else "对登记的水分子固定几何做 RHF/STO-3G 单点，电荷0、多重度1，交付电子能及来源。")
        goal = Goal(id="result_goal", port="optimized_geometry" if methane else "energy",
                    minimum_check_version="orca-hf-2", original_text=text,
                    conditions={"geometry_relation": "optimized" if methane else "fixed_initial"})
        constraints = {}
        if case.startswith("repair_"):
            candidate = 100 if case == "repair_success" else 2
            constraints["initial_parameters"] = {"scf_maxiter": 1}
            text += f"初次SCF MaxIter必须为1；若真实SCF不收敛，只允许把MaxIter提高到{candidate}，最多修复一次。"
            permission.allowed_repairs = {"scf_maxiter": [candidate]}
            budget = agent_budget(orca_starts=2, extra_orca_starts=1, attempts_per_step=2)
        elif methane:
            permission.allowed_tools = ["orca.opt"]
            budget = agent_budget(orca_starts=1, extra_orca_starts=0)
        else:
            budget = agent_budget(orca_starts=1, extra_orca_starts=0)
        request = Request(original_text=text, geometry_artifact_id=artifact.id, **settings,
                          goals=[goal], conditions=constraints, normalization_status="normalized")
    request.conditions["explain_results"] = True
    run = initialize_agent(store, config, request, permission, budget, batch_category=category)
    return run, metadata


def run_case(case, category, identity, *, live_model=False, live_orca=False, resume=False, model_profile=None):
    if not live_model or not live_orca:
        raise ValueError("joint execution requires both explicit live switches")
    if not identity.replace("_", "").replace("-", "").isalnum() or len(identity) > 60:
        raise ValueError("evaluation identity must be a short stable label")
    frozen = None
    config = evaluation_config(science=True, model_profile=model_profile)
    if category == "formal":
        import os

        from tests.helpers.phase_b_freeze import validate_freeze
        frozen = validate_freeze(os.environ.get("ORCA_AGENT_EVAL_FREEZE", "formal-v1"),
                                 config=config, science=True)
    store = Store(ROOT / "agent")
    config = config.model_copy(update={"data_root": store.root})
    metadata_path = ROOT / "evaluations" / f"{identity}.json"
    if metadata_path.exists():
        metadata = _json(metadata_path)
        if (metadata["case"], metadata["category"]) != (case, category):
            raise ValueError("evaluation identity cannot be rebound")
        if metadata.get("model_profile", "disabled") != config.model_profile:
            raise ValueError("joint evaluation model profile cannot change")
        if category == "formal" and metadata.get("freeze") != frozen:
            raise ValueError("joint evaluation is bound to a different immutable formal freeze")
        run = store.load_run(metadata["run_id"])
        if run.batch_category != category:
            raise ValueError("joint Run category differs from its immutable evaluation metadata")
        if not resume:
            return run, metadata
    else:
        run, metadata = prepare_case(store, config, case, category)
        metadata.update(run_id=run.id, data_root=str(store.root), freeze=frozen, model_profile=config.model_profile)
        _write(metadata_path, metadata, immutable=True)
    budget = AcceptanceBudget(store)
    run = execute(store, config, run.id, resume=resume, batch=budget)
    report = build_report(store, run)
    _write(ROOT / "evaluations" / f"{identity}.report.json", report)
    atomic_write(ROOT / "evaluations" / f"{identity}.report.md", render_report(report).encode("utf-8"))
    return run, metadata


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("case", choices=CASES)
    parser.add_argument("--category", choices=("development", "formal"), required=True)
    parser.add_argument("--identity", required=True)
    parser.add_argument("--live-model", action="store_true")
    parser.add_argument("--live-orca", action="store_true")
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--model-profile", choices=("disabled", "thinking_low"),
                        help="explicit model mode; otherwise use the scientific configuration")
    args = parser.parse_args()
    run, _ = run_case(args.case, args.category, args.identity,
                      live_model=args.live_model, live_orca=args.live_orca, resume=args.resume,
                      model_profile=args.model_profile)
    print(json.dumps({"run_id": run.id, "state": run.state, "goal_status": run.goal_status,
                      "usage": run.usage.model_dump(mode="json"), "diagnostics": run.diagnostics},
                     ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
