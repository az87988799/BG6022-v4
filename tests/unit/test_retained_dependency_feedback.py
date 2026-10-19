"""Retained graph diagnostics; real rejected proposal plus offline controls."""

import copy
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from orca_agent.models import Plan, Request, Run
from orca_agent.proposals import ProposalError, materialize_plan

FIXTURE = Path(__file__).parents[1] / "fixtures/code_contracts/live018-missing-retained-dependency.json"


def case():
    assert hashlib.sha256(FIXTURE.read_bytes()).hexdigest() == (
        "3f264844b1814afbd76a2733e4faecaf76fe485b1dbf49e40f00e724bdf085fd")
    data = json.loads(FIXTURE.read_text(encoding="utf-8"))
    prior = Plan.model_validate(data["plan"])
    request = Request.model_validate(data["request"])
    run = Run.model_validate(data["run_context"])
    store = SimpleNamespace(load_plan=lambda _: prior, load_request=lambda _: request)
    return data, prior, request, run, store


def test_actual_live018_missing_retained_dependency_is_locatable_without_repairing():
    data, prior, request, run, store = case()
    proposal = data["proposal"]["parameters"]
    before = copy.deepcopy((prior, request, run, proposal))
    included = {item["key"] for item in proposal["steps"]}
    index, retained = next((index, step) for index, item in enumerate(proposal["steps"])
                          for step in prior.steps if item == {"key": step.id}
                          and any(dependency not in included for dependency in step.depends_on))
    missing = [dependency for dependency in retained.depends_on if dependency not in included]
    with pytest.raises(ProposalError) as caught:
        materialize_plan(store, run, proposal)
    detail = caught.value.detail
    assert detail["code"] == "missing_retained_dependency"
    assert detail["path"] == ["parameters", "steps", index, "key"]
    assert detail["retained_step_id"] == retained.id
    assert detail["missing_dependency_step_ids"] == missing
    assert "not automatically" in str(caught.value)
    assert (prior, request, run, proposal) == before
    assert not run.attempts and not run.calls and not run.usage.model_calls


def test_synthetic_closure_restoration_uses_exact_retained_steps_without_execution():
    data, prior, request, run, store = case()
    proposal = copy.deepcopy(data["proposal"]["parameters"])
    keys = {item["key"] for item in proposal["steps"]}
    missing = {dependency for step in prior.steps if step.id in keys
               for dependency in step.depends_on if dependency not in keys}
    assert missing
    proposal["steps"][:0] = [{"key": key} for key in sorted(missing)]
    before = copy.deepcopy((prior, request, run))
    candidate = materialize_plan(store, run, proposal)
    for old in prior.steps:
        retained = next(step for step in candidate.steps if step.id == old.id)
        assert retained == old and retained is not old
    assert (prior, request, run) == before
    assert not run.attempts and not run.calls and not run.usage.orca_starts_reserved
    # This proves graph materialization only. Other candidate defects still
    # require ordinary activation validation; no real model repair is claimed.
