"""a007 source cardinality failures using immutable inert archives only."""

import pytest

from orca_agent.config import Config
from orca_agent.models import Observation, RunConfiguration, fingerprint
from orca_agent.task_observations import assess
from tests.unit.test_bounded_observation_admission import manifest_case, read_records
from tests.unit.test_explicit_observation_file_sets import file_set_case
from tests.unit.test_task_analysis_traceability import declaration, source_item

POLICY = "reported-observation-2"


def enable(run):
    run.configuration = run.configuration.model_copy(update={"observation_delivery_policy": POLICY})


def metadata_case(tmp_path):
    value = {"seed": 1701, "rdkit_embed_randomSeed": 1701, "forcefield_deterministic": True}
    store, run, request, goal, result, payload = manifest_case(tmp_path, [source_item(value=value, sources=[])])
    enable(run)
    goal.conditions = {}
    goal.quantity = goal.original_text = "Record the selected random seed and its limitations."
    goal.minimum_evidence = ["Record the selected random seed and its limitations."]
    return store, run, request, goal, result, payload


@pytest.mark.parametrize("model", [Config, RunConfiguration])
def test_policy_v2_is_explicit_and_legacy_configuration_serialization_is_unchanged(model):
    old = model().model_dump(mode="json")
    assert "observation_delivery_policy" not in old
    assert model.model_validate(old).model_dump(mode="json") == old
    new = model(observation_delivery_policy=POLICY)
    assert new.observation_delivery_policy == POLICY
    if model is Config:
        assert new.run_configuration().observation_delivery_policy == POLICY
    old_record = Observation(name="seed", value=1701)
    assert "metadata" not in old_record.model_dump(mode="json")
    assert fingerprint(old_record) == fingerprint(old_record.model_dump(mode="json"))


def test_33_precise_sources_fit_frozen_count_without_losing_locations(tmp_path):
    names = ["anchor.xyz", *[f"candidates/candidate_{i:02d}.xyz" for i in range(32)]]
    declarations = [{"filename": name, "location": f"geometry {i}"} for i, name in enumerate(names)]
    store, run, result, payload = file_set_case(tmp_path, names, sources=declarations)
    enable(run)
    # Keep the files but make only sources consume this record's count bound.
    declaration(store, run, result, tmp_path, [source_item(sources=declarations)])
    payload = store.artifact_path(result.source["files"]["task/observations.json"]["artifact_id"]).read_bytes()
    run.budget.artifact_count = 33
    diagnostics = []
    record, = read_records(store, run, result, diagnostics)
    assert len(record.sources) == 33
    assert [row.location for row in record.sources] == [row["location"] for row in declarations]
    assert record.validation_status == "unverified" and not record.metadata
    assert diagnostics[-1]["rule_version"] == "bounded-observation-admission-4"
    assert diagnostics[-1]["record_count_limit"] == 32
    assert diagnostics[-1]["record_bytes_limit"] == 256 * 1024
    assert diagnostics[-1]["total_record_bytes_limit"] == 1024 * 1024
    assert store.artifact_path(diagnostics[-1]["manifest"]["artifact_id"]).read_bytes() == payload
    # Source files, not citations, consume the frozen count allowance.
    run.budget.artifact_count = 32
    result.observation_records = []
    diagnostics = []
    assert read_records(store, run, result, diagnostics) == []
    assert "artifact_count" in diagnostics[0]["detail"]


def test_v2_sources_keep_existing_record_byte_limit_even_without_simplified_workflow(tmp_path):
    item = source_item(sources=[{"filename": "stdout.out", "internal_request_key": "native",
                                 "location": "x" * 1000}] * 260)
    store, run, _, _, result, _ = manifest_case(tmp_path, [item], simplified=False)
    enable(run)
    diagnostics = []
    assert read_records(store, run, result, diagnostics) == []
    assert "per-record byte allowance" in diagnostics[0]["detail"]
    assert diagnostics[-1]["record_bytes_limit"] == 256 * 1024


@pytest.mark.parametrize("policy", [None, "reported-observation-1"])
@pytest.mark.parametrize("count", [0, 17])
def test_previous_policies_keep_the_original_source_count_contract(tmp_path, policy, count):
    item = source_item(sources=[{"filename": "stdout.out", "internal_request_key": "native"}] * count)
    store, run, _, _, result, _ = manifest_case(tmp_path, [item])
    run.configuration = run.configuration.model_copy(update={"observation_delivery_policy": policy})
    diagnostics = []
    assert read_records(store, run, result, diagnostics) == []
    assert "at least 1 and at most 16" in diagnostics[0]["detail"]
    assert diagnostics[-1]["rule_version"] == "bounded-observation-admission-3"


def test_empty_source_value_is_bound_to_exact_manifest_code_and_attempt(tmp_path):
    store, run, request, goal, result, payload = metadata_case(tmp_path)
    diagnostics = []
    record, = read_records(store, run, result, diagnostics)
    result.observation_records = [record]
    source, = record.sources
    manifest = result.source["files"]["task/observations.json"]
    assert (source.artifact_id, source.sha256) == (manifest["artifact_id"], manifest["sha256"])
    assert source.location == "observations[0].value" and source.internal_request_key is None
    assert source.actual_conditions == {"origin": "self_reported_manifest_value"}
    assert record.metadata["self_reported"] is True and record.metadata["declared_sources"] == []
    assert record.metadata["manifest"]["path"][-1] == {"kind": "index", "index": 0}
    producer = record.metadata["producer"]
    assert producer["attempt_id"] == run.attempts[0].id
    assert producer["code_sha256"] == record.code_sha256
    assert producer["frozen_step_fingerprint"] == fingerprint(run.attempts[0].frozen_step)
    assert record.metadata["configuration_fingerprint"] == fingerprint(run.configuration)
    assert record.metadata["observation_delivery_policy"] == POLICY
    assert record.value == {"seed": 1701, "rdkit_embed_randomSeed": 1701, "forcefield_deterministic": True}
    assert record.actual_conditions == {} and record.validation_status == "unverified"
    assert diagnostics[0]["normalizations"][0]["declared_sources"] == []
    assert assess(store, run, request, goal, result)["status"] == "passed"
    assert not result.qualified_outputs
    assert store.artifact_path(manifest["artifact_id"]).read_bytes() == payload


@pytest.mark.parametrize("requirement", ["native", "physical", "qualified", "registered"])
def test_self_report_does_not_satisfy_scientific_or_registered_requirements(tmp_path, requirement):
    store, run, request, goal, result, _ = metadata_case(tmp_path)
    result.observation_records = read_records(store, run, result, [])
    if requirement == "native":
        goal.quantity = "Perform a real ORCA single-point calculation."
    elif requirement == "physical":
        goal.conditions["basis"] = "def2-SVP"
    elif requirement == "qualified":
        goal.verification = "qualified"
    else:
        goal.minimum_evidence.append("converged_scf@1")
    assessment = assess(store, run, request, goal, result)
    assert assessment["status"] == "unresolved", assessment
    assert assessment["scientific_qualification"] is False and not result.qualified_outputs


@pytest.mark.parametrize("damage", ["manifest_hash", "code_hash", "frozen_step"])
def test_self_report_requires_an_unchanged_producer_chain(tmp_path, damage):
    store, run, _, _, result, _ = metadata_case(tmp_path)
    if damage == "manifest_hash":
        result.source["files"]["task/observations.json"]["sha256"] = "f" * 64
        with pytest.raises(ValueError, match="manifest archive binding"):
            read_records(store, run, result, [])
    else:
        step = run.attempts[0].frozen_step
        if damage == "code_hash":
            step.parameters.code.sha256 = "f" * 64
        else:
            run.attempts[0].frozen_step = None
        from orca_agent.task_observations import collect
        diagnostics = []
        assert collect(store, run, step, run.attempts[0], result.source["files"], diagnostics=diagnostics) == []
        assert diagnostics[0]["category"] == "observation_manifest_rejected"


def test_new_policy_cannot_retroactively_insert_a_missing_historical_record(tmp_path):
    store, run, request, goal, result, _ = metadata_case(tmp_path)
    result.observation_records = []
    before = result.model_dump(mode="json")
    assessment = assess(store, run, request, goal, result)
    assert "generic_observation_differs_from_archived_manifest" in assessment["reasons"]
    assert assessment["status"] == "unresolved" and result.model_dump(mode="json") == before
