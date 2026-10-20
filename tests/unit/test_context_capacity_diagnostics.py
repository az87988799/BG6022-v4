"""Capacity failures are persisted before any model reservation or transmission."""

from test_agent import ScriptedTransport, make_run

from orca_agent import agent, context
from orca_agent.config import Config


def test_actual_context_capacity_failure_retains_sizes_in_persisted_run(tmp_path, monkeypatch):
    store, run, _ = make_run(tmp_path)
    build = agent.build_context

    def too_small(request, run, *args, **kwargs):
        # A local copy narrows only the test input envelope. Do not amend the
        # persisted permission/budget or simulate a failed provider call.
        narrow = run.model_copy(deep=True)
        narrow.budget.input_tokens = 1
        return build(request, narrow, *args, **kwargs)

    monkeypatch.setattr(agent, "build_context", too_small)
    transport = ScriptedTransport()
    completed = agent.execute(store, Config(), run.id, transport=transport)
    assert completed.state == "failed"
    diagnostic, = [item for item in completed.diagnostics if item["category"] == "ContextLimitError"]
    sizes = diagnostic["context_capacity"]
    assert sizes["run_input_limit"] == 1
    assert sizes["candidate_count"] == len(sizes["candidates"]) > 0
    assert all(row["input_token_bound"] == row["content_bytes"] + row["framing_bound"]
               for row in sizes["candidates"])
    assert store.load_run(run.id).diagnostics == completed.diagnostics
    assert not transport.sent and not completed.model_records
    assert completed.usage.model_calls == 0 and completed.usage.orca_starts_actual == 0
    assert completed.budget == run.budget
    assert store.load_request(completed) == store.load_request(run)


def test_credential_bearing_exception_metadata_is_not_persisted(tmp_path, monkeypatch):
    store, run, _ = make_run(tmp_path)

    def rejected(*args, **kwargs):
        raise context.ContextLimitError("capacity exhausted",
            diagnostics={"version": "context-capacity-1", "unexpected": "sk-" + "z" * 32})

    monkeypatch.setattr(agent, "build_context", rejected)
    completed = agent.execute(store, Config(), run.id, transport=ScriptedTransport())
    diagnostic, = [item for item in completed.diagnostics if item["category"] == "ContextLimitError"]
    assert "context_capacity" not in diagnostic
    assert "sk-" not in completed.model_dump_json()
