"""Frozen syntax ordering and actual a016/a017 bytes; no computation or models."""

import hashlib
from pathlib import Path

import pytest

from orca_agent.models import CodeRef, NativeInputPolicy, NativeOrcaParameters, ResourceLimits
from orca_agent.orca.native import prepare_native_input, validate_native_input
from orca_agent.orca.native_managed import materialize_managed_input, profile_contract
from orca_agent.store import Store
from orca_agent.tools.managed import native_input_binding
from tests.unit.test_native_managed import POLICY, XYZ, bound, source

ORDER = NativeInputPolicy.model_validate({**POLICY.model_dump(), "property_block_order": "coordinates-first-1"})


def stage(tmp_path, text, *, xyz=XYZ, name="initial.xyz", binding=None, policy=ORDER):
    tmp_path.mkdir(parents=True, exist_ok=True)
    inp, geometry = tmp_path / "source.inp", tmp_path / "source.xyz"
    inp.write_bytes(text.encode("ascii"))
    geometry.write_bytes(xyz)
    parameters = NativeOrcaParameters(input=CodeRef(
        artifact_id="input", sha256=hashlib.sha256(inp.read_bytes()).hexdigest(), language="orca"),
        inputs={"entries": [{"artifact_id": "geometry", "sha256": hashlib.sha256(xyz).hexdigest(),
                             "target_path": name, "purpose": "original geometry"}]})
    result = prepare_native_input(tmp_path / "task", parameters,
        {"input": inp, "geometry": geometry}.__getitem__, ResourceLimits(), policy=policy,
        binding=binding or {**bound(xyz), "workflow_policy": "simplified-1"})
    assert inp.read_bytes() == text.encode("ascii") and geometry.read_bytes() == xyz
    return result, (tmp_path / "task/job.inp").read_bytes()


def test_order_only_moves_exact_complete_block_and_preserves_other_bytes(tmp_path):
    block = "  %eprnmr  # nuclear settings\r\n    Nuclei = 0 # first atom\r\n\r\n  end\r\n"
    text = source(blocks="PLACEHOLDER\n%output PrintLevel Small end").replace("\n", "\r\n")
    text = text.replace("PLACEHOLDER\r\n", block)
    coordinate = "* xyzfile 0 5 initial.xyz\r\n"
    expected = text.replace(block, "", 1).replace(coordinate, coordinate + block)
    result, final = stage(tmp_path, text)
    assert final == expected.encode("ascii") and final.count(block.encode()) == 1
    assert (tmp_path / "task/source-input.inp").read_bytes() == text.encode("ascii")
    assert len(result["materialization"]) == 1
    audit = result["materialization"][0]
    assert audit["field"] == "property_block_order" and audit["rule"] == "coordinates-first-1"
    assert audit["block_sha256"] == hashlib.sha256(block.encode()).hexdigest()
    start, end = audit["original_byte_span"]
    assert text[start:end] == block
    assert audit["before_sha256"] == result["source_input_sha256"] == hashlib.sha256(text.encode()).hexdigest()
    assert audit["after_sha256"] == result["input_sha256"] == hashlib.sha256(final).hexdigest()
    assert result["actual_conditions"]["input_blocks"]["eprnmr"] == {"nuclei": "0"}
    assert result["actual_conditions"]["scientific_validation"] == "unverified"
    assert "Frozen policy moves" in profile_contract(ORDER)["geometry"]
    assert "Blocks stay in declared order" in profile_contract(POLICY)["geometry"]


@pytest.mark.parametrize("block,after", [("%eprnmr Nuclei 0 end\n", True), ("%eprnmr NMR false end\n", False)])
def test_already_after_or_non_nuclear_block_is_exact_noop(tmp_path, block, after):
    text = source() + block if after else source(blocks=block)
    result, final = stage(tmp_path, text)
    assert final == text.encode() and "materialization" not in result
    assert "source_input_sha256" not in result


@pytest.mark.parametrize("controls", ["Nuclei 0", "NMR true"])
def test_old_frozen_policy_still_rejects_order_and_new_policy_is_idempotent(tmp_path, controls):
    text = source(blocks=f"%eprnmr {controls} end")
    with pytest.raises(ValueError, match="eprnmr must follow"):
        stage(tmp_path / "old", text, policy=POLICY)
    assert not (tmp_path / "old/task").exists()
    _, final = stage(tmp_path / "new", text)
    again, records = materialize_managed_input(final.decode(), ResourceLimits(), ORDER,
                                              {**bound(), "workflow_policy": "simplified-1"})
    assert again.encode() == final and records == []


@pytest.mark.parametrize("change", [
    lambda s: s.replace("%eprnmr Nuclei 0 end", "%eprnmr Nuclei 0"),
    lambda s: s.replace("%eprnmr Nuclei 0 end", "%eprnmr Nuclei 0 end\n%eprnmr Nuclei 1 end"),
    lambda s: s.replace("%eprnmr Nuclei 0 end", "%eprnmr Nuclei 0 end trailing 1"),
    lambda s: s + "* xyzfile 0 5 initial.xyz\n",
    lambda s: s + "$new_job\n",
    lambda s: s.replace(" UKS ", " RKS "),
    lambda s: s.replace("%maxcore 192", "%maxcore 2048"),
])
def test_reordering_does_not_relax_structure_conditions_or_resource_rejections(tmp_path, change):
    with pytest.raises(ValueError):
        stage(tmp_path, change(source(blocks="%eprnmr Nuclei 0 end")))
    assert not (tmp_path / "task/job.inp").exists()


@pytest.mark.parametrize("attempt_name,run_id,expected_sha", [
    ("a016", "run_d43e58295336493ebad773115b7644bd", "c39e5831a943c46d5cd9edaa2e9c6d472ef57be79bd8e24efa126d6690cec191"),
    ("a017", "run_1283352d7b94418984d3a6f79db9eea5", "261268d4a5f14dc107a3357e017daff149a5d6af89747fe8460334bebe77f795"),
])
def test_actual_failed_native_sources_stage_with_exact_block_and_bound_geometry(tmp_path, attempt_name, run_id, expected_sha):
    root = Path(__file__).parents[2] / "data/live-repair/attempts" / attempt_name / "data"
    if not (root / "runs" / run_id / "run.json").is_file():
        pytest.skip("local immutable native-input archive is not available")
    store = Store(root)
    run = store.load_run(run_id)
    attempt = run.attempts[0]
    plan = store.load_plan_revision(run, attempt.plan_version)
    view = run.model_copy(update={"plan_id": plan.id, "plan_version": plan.version,
                                  "request_version": attempt.request_version})
    step = next(item for item in plan.steps if item.id == attempt.step_id)
    binding = native_input_binding(store, view, step)
    if attempt_name == "a016":
        original_path = store.artifact_path(attempt.frozen_step.parameters.input.artifact_id)
        geometry_path = store.artifact_path(attempt.frozen_step.parameters.inputs.entries[0].artifact_id)
    else:
        original_path = store.path(attempt.directory) / "sealed/sdk/hcn_giao_nmr-000002/native.inp"
        geometry_path = original_path.parent / "dependency-0"
    original, xyz = original_path.read_bytes(), geometry_path.read_bytes()
    assert hashlib.sha256(original).hexdigest() == expected_sha
    text = original.decode("ascii")
    with pytest.raises(ValueError, match="eprnmr must follow"):
        stage(tmp_path / "old", text, xyz=xyz, name="hcn.xyz", binding=binding,
              policy=store.run_configuration(run).execution.native_input_policy)
    result, final = stage(tmp_path / "new", text, xyz=xyz, name="hcn.xyz", binding=binding)
    audit = next(item for item in result["materialization"] if item["field"] == "property_block_order")
    start, end = audit["original_byte_span"]
    block = original[start:end]
    assert block.startswith(b"%eprnmr") and final.count(block) == 1
    assert final.index(b"* xyzfile") < final.index(block)
    assert result["source_input_sha256"] == expected_sha
    assert result["input_sha256"] == hashlib.sha256(final).hexdigest()
    assert (tmp_path / "new/task/source-input.inp").read_bytes() == original
    assert (tmp_path / "new/task/hcn.xyz").read_bytes() == xyz
    admitted = validate_native_input(final.decode(), {"hcn.xyz": xyz}, ResourceLimits(), policy=ORDER, binding=binding)
    assert admitted["actual_conditions"] == result["actual_conditions"]
    assert result["actual_conditions"]["input_blocks"]["eprnmr"] == {"nuclei": "all"}
    assert any(item["field"] == "cores" for item in result["materialization"])
    assert original_path.read_bytes() == original and geometry_path.read_bytes() == xyz
