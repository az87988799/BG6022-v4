"""Offline regressions for geometry wording and unchanged condition inheritance."""

import pytest
from test_natural import store_at
from test_semantic_control import candidate

from orca_agent.config import Config
from orca_agent.model_usage import current_basis
from orca_agent.models import Goal, PermissionSnapshot, Request, SystemInput
from orca_agent.natural import initialize_agent
from orca_agent.semantic import FieldEvidence, _field, commit_candidate
from orca_agent.store import StoreError
from tests.helpers.phase_b_joint import prepare_case


def observed_request(**values):
    return Request(goals=[Goal(id="read", port="value_observation",
                               minimum_check_version="evidence-read-1")], **values)


def test_joint_methane_raw_text_normalizes_all_conditions_without_execution(tmp_path, monkeypatch):
    from orca_agent import doctor

    monkeypatch.setattr(doctor, "diagnose", lambda _: pytest.fail("normalization must not probe ORCA"))
    store = store_at(tmp_path)
    run, metadata = prepare_case(store, Config(), "methane_opt_control", "development")
    initial = store.load_request(run)
    text = initial.original_text
    assert "气相 RHF/STO-3G 中性单重态无约束优化" in text
    fields = {"method": ("HF", "RHF"), "basis": ("STO-3G", "STO-3G"),
              "charge": (0, "中性"), "multiplicity": (1, "单重态"),
              "environment": ("gas_phase", "气相"), "electronic_state": ("RHF", "RHF")}
    # The proposal is test-authored; the raw bundle/geometry are the actual
    # joint driver inputs. This checks activation, not model understanding.
    parameters = candidate(store, run, kind="normalize", conditions={
        field: {"value": value, "source": "explicit", "text_basis": quote}
        for field, (value, quote) in fields.items()}, goals=[
            {"key": "structure", "port": "optimized_geometry", "system_refs": ["methane"],
             "text_basis": text, "minimum_evidence": []},
            {"key": "energy", "port": "energy", "system_refs": ["methane"],
             "text_basis": text, "geometry_relation": "optimized", "minimum_evidence": []}])
    updated = commit_candidate(store, run, parameters, decision_id="offline_methane_normalization",
                               basis=current_basis(store, run))
    request = store.load_request(updated)
    assert metadata["input_form"] == "raw_text"
    assert request.normalization_status == "normalized" and not request.unresolved
    assert request.systems == initial.systems
    assert all(request.conditions[field] == value and request.conditions_source[field] == "explicit"
               for field, (value, _) in fields.items())
    assert {goal.port for goal in request.goals} == {"optimized_geometry", "energy"}
    assert all(goal.system_ids == ["methane"] and not goal.unresolved for goal in request.goals)
    assert request.goals[1].conditions["geometry_relation"] == "optimized"
    assert updated.plan_id is None and not updated.calls and not updated.attempts and not updated.model_records
    assert updated.usage.model_calls == updated.usage.orca_starts_actual == 0


@pytest.mark.parametrize("phrase", ["without constraints", "without geometric constraints", "unconstrained"])
def test_unconstrained_geometry_does_not_negate_english_reference_conditions(phrase):
    text = f"Optimize neutral singlet methane in gas phase using RHF/STO-3G {phrase}."
    for name, value, quote in [("method", "HF", "RHF"), ("basis", "STO-3G", "STO-3G"),
                               ("charge", 0, "neutral"), ("multiplicity", 1, "singlet"),
                               ("environment", "gas_phase", "gas phase"),
                               ("electronic_state", "RHF", "RHF")]:
        assert _field(name, FieldEvidence(value=value, source="explicit", text_basis=quote),
                      observed_request(), [{"text": text}])[:2] == (value, "explicit")


@pytest.mark.parametrize("text,name,value,quote", [
    ("不采用无约束RHF优化", "method", "HF", "RHF"),
    ("不要用 RHF 做无约束优化", "method", "HF", "RHF"),
    ("不采用 STO-3G 做无约束优化", "basis", "STO-3G", "STO-3G"),
    ("非中性体系做无约束优化", "charge", 0, "中性"),
    ("不是单重态做无约束优化", "multiplicity", 1, "单重态"),
    ("不在气相做无约束优化", "environment", "gas_phase", "气相"),
    ("RHF是否适用未知但需要无约束优化", "electronic_state", "RHF", "RHF"),
    ("do not optimize using RHF without constraints", "method", "HF", "RHF"),
    ("optimize without RHF", "method", "HF", "RHF"),
    ("RHF applicability unknown for optimization without constraints", "method", "HF", "RHF"),
])
def test_geometry_modifier_does_not_remove_real_negation_or_uncertainty(text, name, value, quote):
    with pytest.raises(StoreError, match="negated or uncertain"):
        _field(name, FieldEvidence(value=value, source="explicit", text_basis=quote),
               observed_request(), [{"text": text}])


@pytest.mark.parametrize("target_value,target_source", [
    (None, "unknown"), (1, "inferred"), (3, "explicit"), (None, None),
    (True, "explicit"), (1.0, "explicit"), (1, None),
])
def test_known_source_cannot_assign_an_unconfirmed_or_different_target(target_value, target_source):
    target = SystemInput(id="methane", conditions={"multiplicity": target_value},
                         conditions_source={"multiplicity": target_source} if target_source else {})
    source = SystemInput(id="water", conditions={"multiplicity": 1},
                         conditions_source={"multiplicity": "explicit"})
    request = observed_request(systems=[source, target], multiplicity=1,
                               conditions_source={"multiplicity": "explicit"})
    with pytest.raises(StoreError, match="target scope"):
        _field("multiplicity", FieldEvidence(value=1, source="inherited", request_version=request.version,
                                              system_ref="water"), request,
               [{"text": "甲烷的多重度仍未确定。"}], system=target)


@pytest.mark.parametrize("source_value", [True, 1.0, "1", None])
def test_inheritance_source_also_requires_an_exact_confirmed_integer(source_value):
    target = SystemInput(id="methane", conditions={"multiplicity": 1},
                         conditions_source={"multiplicity": "explicit"})
    source = SystemInput(id="water", conditions={"multiplicity": source_value},
                         conditions_source={"multiplicity": "explicit"})
    request = observed_request(systems=[source, target])
    with pytest.raises(StoreError, match="unique recorded source"):
        _field("multiplicity", FieldEvidence(value=1, source="inherited", request_version=request.version,
                                              system_ref="water"), request, [], system=target)


@pytest.mark.parametrize("origin", ["explicit", "default", "inherited"])
@pytest.mark.parametrize("scope", ["request", "system", "other_confirmed_system"])
def test_inheritance_can_restate_the_same_confirmed_target(origin, scope):
    source = SystemInput(id="water", conditions={"multiplicity": 1},
                         conditions_source={"multiplicity": origin})
    target = SystemInput(id="methane", conditions={"multiplicity": 1} if scope != "request" else {},
                         conditions_source={"multiplicity": origin} if scope != "request" else {})
    request = observed_request(systems=[source, target], multiplicity=1,
                               conditions_source={"multiplicity": origin})
    item = FieldEvidence(value=1, source="inherited", request_version=request.version,
                         system_ref="water" if scope == "other_confirmed_system" else None)
    assert _field("multiplicity", item, request, [], system=target)[:2] == (1, "inherited")


def test_cross_system_inheritance_rejection_preserves_persisted_unknown_and_pending_message(tmp_path):
    store = store_at(tmp_path)
    request = observed_request(systems=[
        SystemInput(id="water", conditions={"multiplicity": 1},
                    conditions_source={"multiplicity": "explicit"}),
        SystemInput(id="methane", conditions={"multiplicity": None},
                    conditions_source={"multiplicity": "unknown"})],
        unresolved=["unconfirmed:methane:multiplicity"])
    run = initialize_agent(store, Config(), request, PermissionSnapshot(model_execution=True))
    message = store.enqueue_message(run.id, "甲烷多重度仍未确定。")
    parameters = candidate(store, run, kind="amend", system_conditions={"methane": {
        "multiplicity": {"value": 1, "source": "inherited", "system_ref": "water",
                         "request_version": request.version}}}, resolves=["unconfirmed:methane:multiplicity"])
    before = store.load_run(run.id).model_dump_json()
    with pytest.raises(StoreError, match="target scope"):
        commit_candidate(store, run, parameters, decision_id="invalid_cross_system_assignment",
                         basis=current_basis(store, run))
    unchanged = store.load_run(run.id)
    assert unchanged.model_dump_json() == before
    assert message not in unchanged.processed_messages
    target = store.load_request(unchanged).systems[1]
    assert target.conditions["multiplicity"] is None and target.conditions_source["multiplicity"] == "unknown"
    assert not unchanged.calls and not unchanged.attempts and not unchanged.model_records


@pytest.mark.parametrize("name,value,invalid", [
    ("charge", 0, False), ("charge", 0, 0.0),
    ("multiplicity", 1, True), ("multiplicity", 1, 1.0),
])
@pytest.mark.parametrize("invalid_scope", ["authorization", "request_prior", "system_prior"])
def test_default_cannot_launder_boolean_or_float_electronic_conditions(name, value, invalid, invalid_scope):
    request = observed_request(semantic_defaults={name: invalid if invalid_scope == "authorization" else value})
    system = None
    if invalid_scope == "request_prior":
        request.conditions[name] = invalid
        request.conditions_source[name] = "default"
    elif invalid_scope == "system_prior":
        system = SystemInput(id="water", conditions={name: invalid}, conditions_source={name: "default"})
        request.systems = [system]
    with pytest.raises(StoreError, match="not authorized|cannot be replaced"):
        _field(name, FieldEvidence(value=value, source="default", default_rule="local-hf-1"),
               request, [], system=system)


@pytest.mark.parametrize("name,value", [("charge", 0), ("multiplicity", 1)])
def test_authorized_exact_integer_default_can_be_repeated_without_changing_value(name, value):
    request = observed_request(semantic_defaults={name: value}, conditions={name: value},
                               conditions_source={name: "default"})
    assert _field(name, FieldEvidence(value=value, source="default", default_rule="local-hf-1"),
                  request, [])[:2] == (value, "default")
