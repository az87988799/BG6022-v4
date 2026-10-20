"""Selection prose is delivered from the Request, never reconstructed by the report."""

from copy import deepcopy

import pytest
from test_report_extended import source as shared_source

from orca_agent.report import build_report, render_report


@pytest.fixture
def source(tmp_path):
    return shared_source.__wrapped__(tmp_path)


# Exact selection prose retained in the a010 Request v3 decision. This fixture
# does not assert scientific suitability and never runs the recorded method.
RECORDED_REASONS = {
    "method": "Composite meta-GGA method with built-in basis, efficient for main-group geometry optimization and harmonic frequency calculations within the 4-core/192 MB per-process budget; suitable for neutral singlet ethanol in gas phase. Selection is model-based, not literature-validated.",
    "basis": "r2SCAN-3c is a self-contained composite method whose basis is included; using the built-in basis avoids a separate orbital-basis choice and is consistent with the managed native-input contract.",
}
USER_AUTHORIZATION = {
    "message_id": "message_eb0ceade81e647f2880430c4a6270718",
    "text_basis": "请为乙醇（中性、单重态、气相）生成结构，在当前资源和预算内自行选择合适的方法和基组并说明依据，完成几何优化和该优化结构上的振动频率计算。",
    "start": 0, "end": 68, "schema": "request-semantics-3", "source": "explicit",
}


def test_recorded_choice_and_provenance_are_delivered_without_changing_scientific_status(source):
    request = source.request
    request.version = source.run.request_version = 3
    request.method, request.basis = "r2SCAN-3c", "built-in"
    request.conditions.update(method=request.method, basis=request.basis)
    request.conditions_source.update(method="auto_selected", basis="auto_selected")
    before_report = build_report(source.store, source.run)
    request.selection_reasons = deepcopy(RECORDED_REASONS)
    for name, reason in RECORDED_REASONS.items():
        request.condition_evidence["request." + name] = {
            "value": getattr(request, name), "source": "auto_selected", "request_version": 2,
            "authorization": deepcopy(USER_AUTHORIZATION), "reason": reason, "sources": [],
            "schema": "condition-selection-3", "basis_kind": "model_reason",
            "scientific_validation": "not_established_by_selection",
        }
    before_request, before_run = request.model_dump(mode="json"), source.run.model_dump(mode="json")
    report = build_report(source.store, source.run)
    rendered = render_report(report)
    assert report["request"]["selection_reasons"] == RECORDED_REASONS
    assert "Request v3 原记录" in rendered
    for name, reason in RECORDED_REASONS.items():
        evidence = report["request"]["selection_evidence"]["request." + name]
        assert evidence == {
            key: value for key, value in request.condition_evidence["request." + name].items()
            if key != "authorization"
        } | {"user_request_authorization": USER_AUTHORIZATION}
        assert reason in rendered
        assert "`request." + name + "` = " + getattr(request, name) in rendered
    assert USER_AUTHORIZATION["message_id"] in rendered
    assert USER_AUTHORIZATION["text_basis"] in rendered
    assert "not_established_by_selection" in rendered
    assert {k: v for k, v in report.items() if k != "request"} == {
        k: v for k, v in before_report.items() if k != "request"}
    assert request.model_dump(mode="json") == before_request
    assert source.run.model_dump(mode="json") == before_run


def test_unrecorded_choice_reasons_or_sources_are_not_invented(source):
    report = build_report(source.store, source.run)
    assert "selection_reasons" not in report["request"]
    assert "条件选择理由" not in render_report(report)
    source.request.selection_reasons = {"method": "Only this reason was recorded."}
    report = build_report(source.store, source.run)
    assert report["request"]["selection_evidence"] == {}
    assert "Only this reason was recorded." in render_report(report)


def test_user_message_provenance_keeps_the_existing_credential_filter(source):
    source.request.selection_reasons = {"method": "Recorded reason"}
    source.request.condition_evidence = {"request.method": {
        "authorization": {"message_id": "message_one", "api_key": "fixture-secret-value"},
        "sources": [{"authorization": "fixture-header-value"}],
    }}
    report = build_report(source.store, source.run)
    evidence = report["request"]["selection_evidence"]["request.method"]
    assert evidence["user_request_authorization"] == {"message_id": "message_one", "api_key": "[redacted]"}
    assert evidence["sources"] == [{"authorization": "[redacted]"}]
    rendered = render_report(report)
    assert "fixture-secret-value" not in rendered
    assert "fixture-header-value" not in rendered
