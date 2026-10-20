"""Actual live020 rejected manifest: clarify the boundary without granting a file."""

import copy
import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from orca_agent import context
from orca_agent.models import InputManifest, RunConfiguration
from orca_agent.proposals import _schema_error
from tests.unit.test_context import objects
from tests.unit.test_decision_purpose_capacity import decoded

FIXTURE = Path(__file__).parents[1] / "fixtures/code_contracts/live020-file-port-errors.json"


def rejected_manifest():
    record = json.loads(FIXTURE.read_text("utf-8"))["records"][0]
    manifest = copy.deepcopy(record["inputs"])
    for entry in manifest["entries"]:
        entry["producer_step_id"] = entry.pop("producer_key")
    return record, manifest


def test_actual_live020_literal_feedback_has_expected_values_and_exact_json_path():
    record, manifest = rejected_manifest()
    original = copy.deepcopy(manifest)
    with pytest.raises(ValidationError) as caught:
        InputManifest.model_validate(manifest)
    detail = _schema_error(caught.value, path=record["path"], tool="python.execute").detail
    error, = [item for item in detail["errors"] if item["type"] == "literal_error"]
    assert error == {"type": "literal_error", "loc": [*record["path"], "entries", 0, "port"],
                     "expected": "'written_artifact' or 'prepared_geometry'"}
    assert manifest == original and manifest["entries"][0]["port"] == "observations"


def test_artifact_collection_is_not_a_file_selector_either():
    record, manifest = rejected_manifest()
    manifest["entries"][0]["port"] = "artifacts"
    with pytest.raises(ValidationError) as caught:
        InputManifest.model_validate(manifest)
    detail = _schema_error(caught.value, path=record["path"], tool="python.execute").detail
    assert any(item.get("expected") == "'written_artifact' or 'prepared_geometry'"
               for item in detail["errors"])


def test_file_port_correction_survives_model_context_and_compact_code_rules():
    record, manifest = rejected_manifest()
    with pytest.raises(ValidationError) as caught:
        InputManifest.model_validate(manifest)
    # Production reports this at the tool-parameter path.
    errors = caught.value.errors(include_input=False, include_url=False)
    for error in errors:
        error["loc"] = ("inputs", *error["loc"])

    class PrefixedError:
        def errors(self, **_):
            return errors

    detail = _schema_error(PrefixedError(), path=record["path"][:-1], tool="python.execute").detail
    instruction = detail["corrections"][0]["instruction"]
    assert "output collections, not file ports" in instruction
    assert "Result.source.files" in instruction and "pending Goals may remain gaps" in instruction
    request, run = objects()
    run.configuration = RunConfiguration(workflow_policy="simplified-1")
    run.permission.allowed_tools = ["workspace.write", "python.execute", "orca.run"]
    run.permission.code_execution = run.permission.artifact_writes = True
    run.budget.input_tokens = 64000
    run.budget.model_tokens = 120000
    prepared = context.build_context(request, run, feedback={"validation_error": {
        "category": "ProposalError", "requirement": detail}})
    wire = decoded(prepared)
    assert "'written_artifact' or 'prepared_geometry'" in json.dumps(wire["CONTROL"]["validation_error"])
    for rules in (wire["TASK_CODE_RULES"], context._capacity_code_rules({"AUTHORITY": {}})):
        assert "port only written_artifact/prepared_geometry" in rules
        assert "not port:artifacts/observations (collections)" in rules
        assert "inspect Result.source.files, then add analysis" in rules
        assert "pending Goals may be gaps" in rules
        assert "Never guess filenames from ports" in rules
