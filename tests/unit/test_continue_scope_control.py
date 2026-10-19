"""Original-scope continuation consumes no HTTP or execution permission."""

import pytest
from test_natural import query_run
from test_semantic_control import candidate

from orca_agent.model_usage import current_basis
from orca_agent.semantic import _continue_command, commit_candidate, deterministic_message
from orca_agent.store import StoreError

TEXT = "继续完成当前已授权任务；保持原目标、条件、最低证据、权限与累计预算。已有脚本和证据继续使用，允许按必要反馈修复。"


def test_real_original_scope_followup_consumes_message_without_changing_contract_or_cost(tmp_path):
    store, run, _, _ = query_run(tmp_path)
    request, plan = store.load_request(run), store.load_plan(run)
    before = run.permission.model_dump(), run.budget.model_dump(), run.usage.model_dump(), run.deadline
    identifier = store.enqueue_message(run.id, TEXT)
    updated, outcome = deterministic_message(store, run)
    assert outcome == "continue" and updated.processed_messages == [identifier]
    assert store.load_request(updated) == request and store.load_plan(updated) == plan
    assert (updated.permission.model_dump(), updated.budget.model_dump(), updated.usage.model_dump(), updated.deadline) == before


@pytest.mark.parametrize("text", ["不要继续当前任务", "继续当前任务；方法改为HF", "继续当前任务；预算增加一倍",
                                "继续当前任务；允许访问网络", "继续当前任务；最低证据改为无要求"])
def test_continuation_grammar_rejects_negation_and_any_scope_or_authority_change(text):
    assert not _continue_command(text)


def test_continue_candidate_cannot_smuggle_changed_condition(tmp_path):
    store, run, _, _ = query_run(tmp_path)
    store.enqueue_message(run.id, TEXT)
    proposed = candidate(store, run, kind="continue", conditions={
        "charge": {"value": 1, "source": "explicit", "text_basis": TEXT}})
    with pytest.raises(StoreError, match="cannot change Request fields"):
        commit_candidate(store, run, proposed, decision_id="changed", basis=current_basis(store, run))
    assert not run.processed_messages
