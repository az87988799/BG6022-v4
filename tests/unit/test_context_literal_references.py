"""Public planning references must not reinterpret historical literal fields."""

import copy

import pytest
from test_context import objects, payload

from orca_agent import context
from orca_agent.delivery import delivery_snapshot


@pytest.mark.parametrize("literal_collision", [False, True])
def test_compact_planning_preserves_current_quotes_and_historical_literal_refs(literal_collision):
    request, run = objects()
    # Repeated provenance makes the real builder choose its compact planning
    # projection; no Store-side decoder or substituted context is involved.
    request.original_text = (
        "Calculate the electronic energy of registered water geometry. "
        "Keep its specified method, basis, charge and multiplicity; "
        "do not reuse unrelated methane conditions. ") * 4
    request.goals[0].text_evidence = {"text_basis": request.original_text, "source": "user"}
    request.condition_evidence = {
        name: {"value": value, "source": "explicit", "text_basis": request.original_text}
        for name, value in {"method": "HF", "basis": "STO-3G", "charge": 0, "multiplicity": 1,
                            "environment": "gas_phase", "electronic_state": "RHF"}.items()}
    historical = {"text_basis": "Methane initial energy.",
                  "text_basis_ref": "AUTHORITY.user_originals[0].text"}
    if literal_collision:
        request.conditions["historical_literal"] = copy.deepcopy(historical)
    feedback = {"historical_observation": {
        "current_request_fields": {"unrelated_methane": ["charge"]}, **historical}}
    before = request.model_dump(mode="json")
    snapshot = delivery_snapshot(request, run, {})
    frozen = copy.deepcopy(snapshot)

    prepared = context.build_context(request, run, delivery_snapshot=snapshot, feedback=feedback)
    decoded = payload(prepared)
    assert "SCHEMA_COLUMNS" in decoded  # Exercise actual compact serialization.
    assert prepared.input_token_bound <= 12000
    visible = decoded["AUTHORITY"]["request"]
    assert visible["condition_evidence"] == before["condition_evidence"]
    assert visible["goals"][0]["text_evidence"] == before["goals"][0]["text_evidence"]
    assert visible["conditions"] == before["conditions"]
    assert decoded["DATA"]["feedback"]["historical_observation"] == feedback["historical_observation"]
    assert decoded["AUTHORITY"]["basis"]["request_version"] == request.version
    assert decoded["AUTHORITY"]["basis"]["permission_version"] == run.permission.version
    assert request.model_dump(mode="json") == before and snapshot == frozen
    declaration = "Request.text_basis_ref=text_basis at that path."
    assert (declaration in prepared.body()["messages"][0]["content"]) is not literal_collision
