"""Offline sizing of unchanged synthetic sampling facts under the frozen budget."""

import hashlib
import json
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(PROJECT))
sys.path.insert(0, str(PROJECT / "tests/unit"))

import pytest
import test_context
from orca_agent.config import load_config
from orca_agent.context import ContextLimitError, build_context


def digest(value):
    return hashlib.sha256(json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def facts(request, run, plan, results, feedback):
    return {
        "request": request.model_dump(mode="json"),
        "run_except_configuration_and_budget": run.model_dump(
            mode="json", exclude={"configuration", "budget"}),
        "plan": plan.model_dump(mode="json"),
        "results": [item.model_dump(mode="json") for item in results],
        "feedback": feedback,
    }


class Captured(Exception):
    pass


def case(phase, profile, config):
    captured = {}

    def capture(request, run, plan, **kwargs):
        captured.update(request=request, run=run, plan=plan, **kwargs)
        raise Captured()

    # Reuse the existing fixture byte-for-byte. Stop at context construction.
    # Its helper registration is already patched by the fixture, with no doctor,
    # transport, scientific subprocess, or result promotion.
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(test_context, "build_context", capture)
        try:
            test_context.test_actual_joint_sampling_ids_four_sp_and_two_analysis_results_fit_without_dropping_feedback(
                Path(tempfile.mkdtemp(prefix="sampling-runtime-budget-")),
                patch, phase, profile)
        except Captured:
            pass
    request, before, plan = (captured[name] for name in ("request", "run", "plan"))
    results, feedback = captured["results"], captured["feedback"]
    original_serialized = before.model_dump_json()
    original_facts = facts(request, before, plan, results, feedback)
    try:
        build_context(request, before, plan, results=results, feedback=feedback,
                      model_profile=profile)
    except ContextLimitError as exc:
        historical_limit = {"status": "known_capacity_failure", "diagnostic": str(exc)}
    else:
        raise AssertionError("The explicitly small historical-budget failure changed")

    current = before.model_copy(deep=True)
    current.configuration = config.run_configuration()
    current.budget = config.text.budget.model_copy(deep=True)
    assert facts(request, current, plan, results, feedback) == original_facts
    prepared = build_context(request, current, plan, results=results, feedback=feedback,
                             model_profile=profile)
    shown = test_context.payload(prepared)
    assert prepared.input_token_bound <= config.model.input_tokens
    assert prepared.input_token_bound <= current.budget.input_tokens
    assert prepared.reserved_tokens <= current.budget.model_tokens - current.usage.model_tokens_used
    assert [item["result_id"] for item in shown["DATA"]["results"]] == [item.id for item in results]
    assert shown["CONTROL"]["validation_error"] == feedback["validation_error"]
    assert shown["AUTHORITY"]["request"]["goals"][0]["conditions"] == request.goals[0].conditions
    for view, source in zip(shown["DATA"]["results"], results, strict=True):
        if "analysis" not in source.observations:
            energy = view["qualified_outputs"]["energy"]
            assert energy["value"] == source.qualified_outputs["energy"].value
            assert energy["unit"] == "Eh"
            checks = energy["checks"]
            if isinstance(checks, list):
                assert len(checks) == 9 and all(item["status"] == "passed" for item in checks)
            elif "names" in checks:
                assert checks["all_passed"]
                assert checks["names"] == [item.name for item in source.qualified_outputs["energy"].checks]
                assert checks["rule_versions"] == ["orca-hf-2"]
            else:
                assert checks["all_passed"] and checks["checked_count"] == 9
            if "source_record_sha256" in view:
                assert view["source_record_sha256"] == digest(source.source)
            elif "source_record_sha256" in view["source"]:
                assert view["source"]["source_record_sha256"] == digest(source.source)
            else:
                assert view["source"]["sha256"] == source.source["sha256"]
            continue
        analysis = view["unqualified_observations"]["analysis"]
        expected = source.observations["analysis"]
        assert len(analysis["members"]) == 5 and analysis["reason"] == expected["reason"]
        assert [item.get("energy_eh") for item in analysis["members"]] == [
            item["energy_eh"] for item in expected["members"]]
    if phase == "pending_analysis":
        ready = shown["AUTHORITY"]["plan"]["steps"][-1]
        assert "immutable_frozen" not in ready
        assert ready["inputs"] == plan.steps[-1].model_dump(mode="json", exclude_none=True)["inputs"]
    assert facts(request, before, plan, results, feedback) == original_facts
    assert before.model_dump_json() == original_serialized
    return {
        "phase": phase, "model_profile": profile,
        "historical_run_input_tokens": before.budget.input_tokens,
        "historical_run_output_tokens": before.budget.output_tokens,
        "historical_model_input_tokens": before.configuration.model.input_tokens,
        "historical_limit": historical_limit,
        "frozen_run_input_tokens": current.budget.input_tokens,
        "frozen_model_input_tokens": current.configuration.model.input_tokens,
        "frozen_output_tokens": current.budget.output_tokens,
        "input_token_bound": prepared.input_token_bound,
        "reserved_tokens": prepared.reserved_tokens,
        "fact_sha256_before_and_after": digest(original_facts),
        "fact_and_source_objects_unchanged": True,
        "result_count": len(results),
        "checks": "Result IDs, energies/units/nine checks, source provenance, all five sampling members, "
                  "feedback, goal conditions and pending analysis bindings retained.",
        "status": "passed",
    }


def main():
    runtime_path = Path(__file__).parents[1] / "freeze/runtime.toml"
    config = load_config(runtime_path)
    report = {
        "recorded_at_utc": datetime.now(timezone.utc).isoformat(),
        "scope": "Offline context construction only. No model, HTTP, ORCA, reference or acceptance score.",
        "runtime_path": str(runtime_path),
        "runtime_sha256": hashlib.sha256(runtime_path.read_bytes()).hexdigest(),
        "script_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "cases": [case(phase, profile, config)
                  for profile in ("disabled", "thinking_low")
                  for phase in ("final", "pending_analysis")],
        "limitations": [
            "The original 12K tests remain known failures; no production optimization or budget change was made.",
            "Each probe copies the complete already frozen configuration/budget into an in-memory Run copy only.",
            "disabled is an offline comparator; thinking_low is the selected real-run profile.",
            "Context fitting is not evidence that the task or scientific calculation succeeds.",
        ],
    }
    output = Path(__file__).with_name("sampling-runtime-budget-probe.json")
    with output.open("x", encoding="utf-8", newline="\n") as stream:
        json.dump(report, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
    print(json.dumps({"path": str(output), "cases": report["cases"]}))


if __name__ == "__main__":
    main()
