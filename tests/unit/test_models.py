import pytest
from pydantic import ValidationError

from orca_agent.models import (
    CalculationParameters,
    Check,
    Goal,
    InputRef,
    OutputBinding,
    Plan,
    QualifiedOutput,
    Request,
    Step,
)


def request_plan():
    request = Request(
        geometry_artifact_id="geometry1", goals=[Goal(id="energy", port="energy")]
    )
    step = Step(
        id="sp1", logical_id="energy", tool="orca.sp",
        geometry=InputRef(artifact_id="geometry1"),
    )
    plan = Plan(
        request_id=request.id, steps=[step],
        goal_map={"energy": OutputBinding(step_id="sp1", port="energy")},
    )
    return request, plan


@pytest.mark.parametrize("values", [
    {"method": "B3LYP"}, {"basis": "def2-TZVP"}, {"charge": 1}, {"multiplicity": 3},
    {"cores": 5}, {"cores": True}, {"memory_mb": 2048}, {"maxcore_mb": 193},
    {"scf_maxiter": 0}, {"opt_maxiter": 100000}, {"timeout_seconds": float("inf")},
    {"raw_input": "! HF\n%pal nprocs 100 end"}, {"shell": "echo unsafe"},
    {"memory_mb": 512, "cores": 4},
])
def test_parameters_reject_unsupported_and_unbounded(values):
    with pytest.raises(ValidationError):
        CalculationParameters(**values)


@pytest.mark.parametrize("values", [
    {}, {"producer_step_id": "opt1"}, {"port": "optimized_geometry"},
    {"artifact_id": "g1", "producer_step_id": "opt1", "port": "optimized_geometry"},
    {"artifact_id": "../../escape"}, {"producer_step_id": "opt1", "port": "energy"},
])
def test_input_binding_is_concrete_or_complete_future_reference(values):
    with pytest.raises(ValidationError):
        InputRef(**values)


def test_plan_request_versions_and_goal_meaning_are_fixed():
    request, plan = request_plan()
    plan.validate_request(request)
    wrong_request = request.model_copy(update={"version": 2})
    with pytest.raises(ValueError, match="revision"):
        plan.validate_request(wrong_request)
    altered_goal = request.model_copy(update={"goals": [Goal(id="energy", port="optimized_geometry")]})
    with pytest.raises(ValueError, match="physical quantity"):
        plan.validate_request(altered_goal)


def test_static_opt_sp_is_valid_and_cycles_are_rejected():
    opt = Step(id="opt", logical_id="geometry", tool="orca.opt", geometry=InputRef(artifact_id="g"))
    sp = Step(
        id="sp", logical_id="energy", tool="orca.sp", depends_on=["opt"],
        geometry=InputRef(producer_step_id="opt", port="optimized_geometry"),
    )
    values = {"request_id": "req", "steps": [opt, sp],
              "goal_map": {"e": OutputBinding(step_id="sp", port="energy")}}
    assert Plan(**values).steps[1].depends_on == ["opt"]
    opt.depends_on = ["sp"]
    with pytest.raises(ValidationError, match="cyclic"):
        Plan(**values)


def test_future_input_requires_explicit_dependency_and_declared_port():
    _, plan = request_plan()
    step = Step(
        id="consumer", logical_id="consumer", tool="orca.sp",
        geometry=InputRef(producer_step_id="sp1", port="optimized_geometry"),
    )
    values = plan.model_dump()
    values["steps"].append(step.model_dump())
    with pytest.raises(ValidationError, match="explicit dependency"):
        Plan.model_validate(values)
    values["steps"][1]["depends_on"] = ["sp1"]
    with pytest.raises(ValidationError, match="does not declare"):
        Plan.model_validate(values)


@pytest.mark.parametrize("status", ["failed", "unverified", "not_applicable"])
def test_unqualified_values_cannot_enter_scientific_ports(status):
    with pytest.raises(ValidationError, match="checks? to pass"):
        QualifiedOutput(value=-75, unit="Eh", checks=[Check(name="SCF", status=status)])


def test_checked_numeric_output_requires_units_and_finite_value():
    checks = [Check(name="SCF", status="passed")]
    for kwargs in ({"value": float("nan"), "unit": "Eh"}, {"value": -75}, {}):
        with pytest.raises(ValidationError):
            QualifiedOutput(checks=checks, **kwargs)


def test_goal_cannot_weaken_rule_or_remove_all_mandatory_evidence():
    with pytest.raises(ValidationError):
        Goal(id="e", port="energy", minimum_check_version="unchecked")
    with pytest.raises(ValidationError, match="mandatory goal"):
        Request(geometry_artifact_id="g", goals=[Goal(id="e", port="energy", required=False)])
