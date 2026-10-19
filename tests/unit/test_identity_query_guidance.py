"""Offline retained intake replay; alias edits are explicitly test-authored."""

import copy
import hashlib
import json
from pathlib import Path

import pytest
from test_general_intake_correction import environment

from orca_agent.model_usage import current_basis
from orca_agent.semantic import commit_candidate
from orca_agent.tools.structure import _requirement

FIXTURE = Path(__file__).parents[1] / "fixtures/code_contracts/live026-030-identity-query.json"
FIXTURE_SHA256 = "8c7b9d3b2b65f617e2ad0208fa5a4599164aa316612decbe41051ad31e0ae388"


@pytest.mark.parametrize("index", [0, 1])
@pytest.mark.parametrize("test_authored_alias", [False, True])
def test_real_normalization_preserves_original_name_and_does_not_verify_lookup_alias(
        tmp_path, index, test_authored_alias):
    raw = FIXTURE.read_bytes()
    assert hashlib.sha256(raw).hexdigest() == FIXTURE_SHA256
    retained = json.loads(raw)["cases"][index]
    assert retained["failed_result"]["source"]["http_status"] == 404
    assert not retained["failed_result"]["qualified_outputs"]
    assert "query_name" not in retained["parameters"]["identities"][0]
    store, _, run = environment(tmp_path, text=retained["user_text"])
    message_id = store.read_control(run.id)["messages"][0]["id"]

    def rebind(value):
        # Only local message identities change in historical replay.
        if isinstance(value, dict):
            return {key: message_id if key == "message_id" else [message_id] if key == "message_ids"
                    else rebind(item) for key, item in value.items()}
        return [rebind(item) for item in value] if isinstance(value, list) else value

    parameters = rebind(copy.deepcopy(retained["parameters"]))
    if test_authored_alias:
        parameters["identities"][0]["query_name"] = "ethanol"
    before = copy.deepcopy(parameters)
    run = commit_candidate(store, run, parameters, decision_id="offline_replay", basis=current_basis(store, run))
    request = store.load_request(run)
    identity = request.systems[0].identity
    assert parameters == before and FIXTURE.read_bytes() == raw
    assert identity["requested_names"] == retained["frozen_identity"]["requested_names"]
    assert identity["canonical_names"] == [] and "alias_verified" not in identity
    expected_query = "ethanol" if test_authored_alias else retained["frozen_identity"]["query_name"]
    assert identity["query_name"] == expected_query
    assert identity["query_name_source"] == ("inferred_translation_or_alias" if test_authored_alias else "explicit")
    assert _requirement(store, run, request.systems[0].id)[2] == expected_query
    assert not run.calls and not run.attempts and not run.result_ids
