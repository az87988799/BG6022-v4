"""Provider accounting failures remain fatal and do not erase scientific facts."""

import copy

import pytest

from orca_agent.llm import proposal_recovery_kind
from orca_agent.models import Goal, Request
from orca_agent.report import build_report, render_report
from orca_agent.store import Store


def test_insufficient_balance_is_fatal_even_if_an_untrusted_retry_flag_claims_otherwise():
    for simplified in (False, True):
        assert proposal_recovery_kind({"error_category": "insufficient_balance", "retryable": True},
                                      workflow_simplified=simplified) == "fatal"


@pytest.mark.parametrize("category", ["insufficient_balance", "http_error"])
def test_current_and_legacy_402_reports_explain_service_recovery_without_rewriting_usage(tmp_path, category):
    store = Store(tmp_path / "data", environment_root=tmp_path / "environment")
    request = Request(goals=[Goal(id="energy", port="energy", minimum_check_version="orca-hf-2")])
    run = store.create_run(request, None)
    run.state = "failed"
    run.usage.model_calls = 1
    run.usage.model_tokens_used = 19
    run.usage.model_tokens_unknown = 120
    run.model_records = [{"id": "model_balance", "status": "unknown", "error_category": category,
                          "http_status": 402, "input_reserved": 100, "output_reserved": 20}]
    before = copy.deepcopy(run)
    report = build_report(store, run)
    assert report["stop_reason"]["model_errors"] == [{"id": "model_balance", "status": "unknown",
        "error_category": category, "http_status": 402}]
    assert report["stop_reason"]["model_service"]["category"] == "insufficient_balance"
    assert report["stop_reason"]["model_service"]["required_action"] == "restore_provider_service"
    rendered = render_report(report)
    assert "余额不足" in rendered and "恢复服务" in rendered and "402" in rendered
    assert report["user_goal_complete"] is False
    assert run == before
    # A later successful model exchange must not be described as currently blocked
    # by a historical provider failure, whose original category still remains.
    run.model_records.append({"id": "model_later", "status": "known"})
    assert "model_service" not in build_report(store, run)["stop_reason"]

