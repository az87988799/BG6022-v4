"""Rebuild one historical request in memory; no model/Tool/process or Run writes."""
import copy
import hashlib
import json
import sys
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from orca_agent import context
from orca_agent.models import Request, Run, fingerprint
from orca_agent.tools.registry import get_tool
from tests.unit.test_context import payload

base = ROOT / 'data/live-repair/attempts/a027/data/runs/run_bcb0cc84ad554e99af47183b52ee25ba'
model_id = 'model_5ef27aad932a423dbd66a0a1fa67e30e'
tracked = {}


def digest(value):
    return hashlib.sha256(value).hexdigest()


def read(path):
    raw = path.read_bytes()
    tracked[str(path)] = digest(raw)
    return json.loads(raw)


# The active coordinator may append Run facts concurrently. Capture it once,
# then reconstruct the historical basis in memory; only immutable originals
# are checked for equality below, not a running coordinator's mutable Run.
run_bytes = (base / 'run.json').read_bytes()
run = Run.model_validate(json.loads(run_bytes))
record = next(item for item in run.model_records if item['id'] == model_id)
now = datetime.fromisoformat(record['created_at'].replace('Z', '+00:00'))
body_path = base / 'model' / f'{model_id}.request.json'
body = read(body_path)
assert tracked[str(body_path)] == record['request_hash']
old_view = payload(SimpleNamespace(body=lambda: body))
snapshot = read(base / 'model' / f'{model_id}.delivery.json')
assert tracked[str(base / 'model' / f'{model_id}.delivery.json')] == record['delivery_snapshot_sha256']
decision = read(base / 'decisions/model_bfaa0bc161be49528ed8aca25f95aa1a.json')
request = Request.model_validate(decision['request'])
assert request.version == record['basis']['request_version'] == 3
assert record['basis']['plan_version'] is None
prior = [item for item in run.model_records if item['created_at'] < record['created_at']]
prior_ids = {item['id'] for item in prior}
run.model_records = prior
run.decisions = [item for item in run.decisions if item['id'] in prior_ids]
run.request_id, run.request_version = request.id, request.version
run.plan_id = run.plan_version = None
run.attempts = [item for item in run.attempts if item.created_at < now]
run.calls = [item for item in run.calls if item.created_at < now]
assert not run.attempts and not run.calls
run.internal_calls = []
run.result_ids = []
run.input_bindings = {}
run.processed_feedback = []
run.goal_status = {item['goal_id']: 'pending' for item in snapshot['goals']}
for field, value in snapshot['resources']['usage'].items():
    if field in type(run.usage).model_fields:
        setattr(run.usage, field, value)
run.usage.model_calls = len(prior)
run.usage.model_tokens_used = sum(item.get('total_tokens', 0) for item in prior)
run.usage.model_tokens_unknown = 0
run.usage.decision_rounds = len(prior)
run.control_generation = record['basis']['control_generation']
run.state = 'ready'
before = copy.deepcopy((run, request, snapshot))
stages = []
prepare = context._prepare_context_request


def visible_tools(wire):
    return [{key: tool[key] for key in ('name', 'named_observation_collector', 'output_contract')}
            for tool in wire.get('TOOLS', []) if 'named_observation_collector' in tool]


def capture(messages, **kwargs):
    wire = payload(SimpleNamespace(body=lambda: {'messages': messages}))
    if 'TOOLS' in wire:
        stages.append(visible_tools(wire))
    return prepare(messages, **kwargs)


context._prepare_context_request = capture
try:
    prepared = context.build_context(request, run, relevant_tools=run.permission.allowed_tools,
        feedback={'pending_step_ids': old_view['CONTROL'].get('pending_step_ids', [])},
        now=now, model_profile=record['model_profile'], delivery_snapshot=snapshot,
        control_generation=run.control_generation)
finally:
    context._prepare_context_request = prepare
view = payload(prepared)
final = visible_tools(view)
assert {item['name'] for item in final} == {'python.execute', 'orca.run'}
for stage in [*stages, final]:
    assert {item['name'] for item in stage} == {'python.execute', 'orca.run'}
    for item in stage:
        definition = get_tool(item['name'])
        assert item['named_observation_collector'] == definition.check_contract['named_observation_collector']
        assert item['output_contract'] == definition.parameter_schema['properties']['observation_names']['description']
assert view['AUTHORITY']['permission'] == old_view['AUTHORITY']['permission']
assert prepared.input_token_bound <= run.budget.input_tokens == 64000
assert before == (run, request, snapshot)
assert all(digest(Path(path).read_bytes()) == sha for path, sha in tracked.items())
output = {'scope': 'Offline counterfactual reconstruction of a027 fourth model request; a027 frozen v91 is unchanged. No model/Tool/ORCA execution.',
    'run_id': run.id, 'model_id': model_id, 'prompt_version': context.PROMPT_VERSION,
    'basis': record['basis'], 'active_run_capture_sha256': digest(run_bytes),
    'active_run_capture_note': 'Read once; ongoing coordinator is permitted to change Run. No Run file was written.',
    'old_input_reserved': record['input_reserved'], 'new_input_token_bound': prepared.input_token_bound,
    'input_limit': run.budget.input_tokens, 'old_collector_occurrences': json.dumps(old_view).count('named_observation_collector'),
    'prepared_stages': stages, 'final_sent_body_tools': final,
    'final_body_sha256': digest(json.dumps(prepared.body(), ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()),
    'permission_unchanged': True, 'in_memory_inputs_unchanged': True,
    'immutable_original_hashes': tracked, 'immutable_originals_unchanged': True,
    'production_hashes': {name: digest((ROOT / name).read_bytes()) for name in
        ['src/orca_agent/readable_context.py', 'src/orca_agent/context.py', 'src/orca_agent/tools/registry.py']}}
destination = ROOT / 'data/workflow-root-repair/a027-capability-context-replay.json'
with destination.open('x', encoding='utf-8') as stream:
    json.dump(output, stream, ensure_ascii=False, indent=2)
print(json.dumps({'path': str(destination), 'sha256': digest(destination.read_bytes()),
    'input_bound': prepared.input_token_bound, 'limit': run.budget.input_tokens,
    'immutable_originals_unchanged': True}))
