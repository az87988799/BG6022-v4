"""Read activated originals only; never execute tools, generated code or writes to Run."""
import hashlib
import json
from pathlib import Path

from pydantic import ValidationError

from orca_agent.models import Plan, fingerprint
from orca_agent.planning import PlanningError, _goal_bindings
from orca_agent.proposals import ProposalError, materialize_plan
from orca_agent.store import Store
from orca_agent.task_observations import assess
from orca_agent.tools.registry import declared_outputs

base = Path('data/live-repair/attempts/a026/data').resolve()
run_id = 'run_4a66f91e2a9b4c999297d7847f7ef1f2'
store = object.__new__(Store)
store.root = base
original_read = store._read_json
before = {}


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def read(relative):
    path = store.path(relative)
    before.setdefault(str(path), digest(path))
    return original_read(relative)


store._read_json = read
run = store.load_run(run_id)
request = store.load_request(run)
plan = store.load_plan(run)
decision = read(f'runs/{run_id}/decisions/model_2c6e4dbcac3145bca2ff03447351ff43.json')
assert fingerprint(plan) == fingerprint(decision['plan'])
run_before = fingerprint(run)
rows = []
for goal in request.goals[:2]:
    step_id = plan.goal_map[goal.id].step_id
    attempt = next(a for a in reversed(run.attempts) if a.step_id == step_id)
    result = store.load_result(run.id, attempt.result_id)
    outcome = assess(store, run, request, goal, result)
    assert outcome['reasons'] == ['generic_observation_requires_task_manifest']
    assert outcome['scientific_qualification'] is False
    rows.append({'goal_id': goal.id, 'attempt_id': attempt.id, 'result_id': result.id,
        'declared_outputs': declared_outputs(attempt.frozen_step),
        'frozen_observation_names': attempt.frozen_step.parameters.observation_names,
        'actual_observation_names': [item.name for item in result.observation_records],
        'assessment': outcome})

try:
    Plan.model_validate(decision['plan'])
except ValidationError as error:
    structural = [{'type': e['type'], 'loc': list(e['loc']), 'msg': e['msg']}
                  for e in error.errors(include_input=False, include_context=False)]
else:
    raise AssertionError('A new direct native value route was admitted')
try:
    _goal_bindings(request, plan, run)
except PlanningError as error:
    commit_rejection = error.detail
    assert commit_rejection['code'] == 'goal_output_route_mismatch'
else:
    raise AssertionError('A historical false value route bypassed current commit checks')

proposal = {'steps': [{'key': step.id} for step in plan.steps], 'goal_map': {
    goal_id: {'step_key': binding.step_id, 'port': binding.port}
    for goal_id, binding in plan.goal_map.items()}}
try:
    materialize_plan(store, run, proposal)
except ProposalError as error:
    proposal_rejection = error.detail
    assert proposal_rejection['code'] == 'goal_output_route_mismatch'
else:
    raise AssertionError('A key-only carry of the false native value route was admitted')

assert fingerprint(run) == run_before
assert all(digest(Path(path)) == sha for path, sha in before.items())
sources = ['src/orca_agent/' + part for part in [
    'tools/registry.py', 'models.py', 'store.py', 'planning.py', 'proposals.py', 'task_observations.py']]
report = {'kind': 'read_only_current_code_replay', 'run_id': run.id,
    'original_state': run.state, 'request_version': request.version, 'plan_version': plan.version,
    'historical_plan_load_exact': True, 'new_plan_structural_rejection': structural,
    'current_commit_rejection': commit_rejection,
    'proposal_replay_scope': 'Key-only carry reconstructed from exact activated Plan; no task bodies executed.',
    'current_proposal_rejection': proposal_rejection, 'rows': rows,
    'originals_unchanged': True, 'run_object_unchanged': True,
    'source_hashes': {path: digest(Path(path)) for path in sources}, 'files': before,
    'conclusion': 'Current routes reject native raw archives as named values and require explicit sourced task-manifest records. Historical Plan remains readable; no old Result, scientific qualification or Run status was changed.'}
destination = Path('data/workflow-root-repair/a026-native-goal-route-after.json')
with destination.open('x', encoding='utf-8') as stream:
    json.dump(report, stream, ensure_ascii=False, indent=2)
    stream.write('\n')
print(json.dumps({'path': str(destination), 'sha256': digest(destination),
                  'bytes': destination.stat().st_size, 'original_files': len(before),
                  'originals_unchanged': True}, ensure_ascii=False))
