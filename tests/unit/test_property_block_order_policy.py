"""Frozen opt-in policy shape; no native input, SDK, or calculation is executed."""

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from orca_agent.models import (
    CodeRef,
    ExecutionConfiguration,
    NativeInputPolicy,
    NativeOrcaParameters,
    fingerprint,
)


def test_missing_property_order_preserves_historical_policy_and_configuration_hashes():
    for profile in ("bounded-analytic-1", "managed-native-1"):
        historical_policy = {"profile": profile, "max_atoms": 32, "max_geom_iterations": 128,
                             "max_scf_iterations": 256, "max_response_iterations": 128}
        policy = NativeInputPolicy.model_validate(historical_policy)
        assert policy.property_block_order is None
        assert policy.model_dump(mode="json") == historical_policy
        assert json.loads(policy.model_dump_json()) == historical_policy
        assert fingerprint(policy) == fingerprint(historical_policy)
        historical_execution = {"backend": "linux-container", "image_id": "sha256:" + "a" * 64,
            "manifest_sha256": "b" * 64, "docker_context": "desktop-linux", "docker_path": str(Path("E:/docker.exe")),
            "native_input_policy": historical_policy}
        execution = ExecutionConfiguration.model_validate(historical_execution)
        assert execution.model_dump(mode="json") == historical_execution
        assert fingerprint(execution) == fingerprint(historical_execution)
        assert "property_block_order" not in NativeInputPolicy(
            profile=profile, property_block_order=None).model_dump()


def test_explicit_property_order_is_persisted_frozen_and_not_a_task_parameter():
    policy = NativeInputPolicy(profile="managed-native-1", property_block_order="coordinates-first-1")
    serialized = policy.model_dump(mode="json")
    assert serialized["property_block_order"] == "coordinates-first-1"
    assert NativeInputPolicy.model_validate(serialized) == policy
    assert fingerprint(policy) != fingerprint(NativeInputPolicy(profile="managed-native-1"))
    with pytest.raises(ValidationError, match="frozen"):
        policy.property_block_order = None
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        NativeOrcaParameters(input=CodeRef(artifact_id="input_fixture", sha256="a" * 64, language="orca"),
                             property_block_order="coordinates-first-1")


def test_property_order_cannot_upgrade_legacy_profile_or_select_unknown_rule():
    with pytest.raises(ValidationError, match="requires managed-native-1"):
        NativeInputPolicy(property_block_order="coordinates-first-1")
    with pytest.raises(ValidationError, match="coordinates-first-1"):
        NativeInputPolicy(profile="managed-native-1", property_block_order="unregistered-order-2")
