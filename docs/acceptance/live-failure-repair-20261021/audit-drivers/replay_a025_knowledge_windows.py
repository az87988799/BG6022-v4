"""Read-only original evidence and counterfactual in-memory context; no GET/model/Tool."""
import copy
import hashlib
import json
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'tests/unit'))
from test_context import payload
from orca_agent.context import PROMPT_VERSION, build_context
from orca_agent.models import Plan, Request, Result, Run
from orca_agent.tools.knowledge import _ManualLinks, _Text, matched_excerpt

BASE = ROOT / 'data/live-repair/attempts/a025/data'
DIRECTORY = BASE / 'runs/run_9d9ed6dbc81245f486a47498af1c8d00'
MODEL = 'model_394b3f7965ff4b9badd3531c877c318c'
RESULT = 'result_ab87a1ee81474b5585c2e38cf3956628'
tracked = {}

def raw(path):
    value = path.read_bytes()
    tracked[str(path)] = hashlib.sha256(value).hexdigest()
    return value

def load(path):
    return json.loads(raw(path))

run = Run.model_validate(load(DIRECTORY / 'run.json'))
record = next(item for item in run.model_records if item['id'] == MODEL)
now = datetime.fromisoformat(record['created_at'].replace('Z', '+00:00'))
old_body = load(DIRECTORY / 'model' / f'{MODEL}.request.json')
old_context = payload(type('Body', (), {'body': lambda self: old_body})())
snapshot = load(DIRECTORY / 'model' / f'{MODEL}.delivery.json')
request = plan = None
for summary in run.decisions:
    if summary.get('request_version') == 3 or summary.get('plan_version') == 1:
        decision = load(DIRECTORY / 'decisions' / (summary['id'] + '.json'))
        if (candidate := decision.get('request')) and candidate['version'] == 3:
            request = Request.model_validate(candidate)
        if (candidate := decision.get('plan')) and candidate['version'] == 1:
            plan = Plan.model_validate(candidate)
assert request and plan
results = [Result.model_validate(load(path)) for path in (DIRECTORY / 'results').glob('*.json')]
results = [result for result in results if result.created_at < now]
target = next(result for result in results if result.id == RESULT)
document = target.observations['document_excerpt']
artifact = load(BASE / 'artifacts' / document['document_artifact_id'] / 'artifact.json')
html_path = BASE / 'artifacts' / document['document_artifact_id'] / 'files/document.html'
html = raw(html_path)
assert hashlib.sha256(html).hexdigest() == document['document_sha256'] == artifact['sha256']
parser = _Text()
parser.feed(html.decode('utf8'))
parser.flush()
windows = []
excerpt, indices = matched_excerpt(parser.parts, document['query'], row_labels=parser.row_labels, windows=windows)
assert any(line.lstrip().startswith('Nuclei =') for line in excerpt.splitlines())
assert len(excerpt) <= 5400 and all(row['end_character'] - row['start_character'] <= 1800 for row in windows)
replay = copy.deepcopy(document)
replay.update(excerpt=excerpt, excerpt_sha256=hashlib.sha256(excerpt.encode()).hexdigest(),
              rendering_version='html-readable-3', excerpt_version='query-window-1')
replay['coverage'].update(windows=windows, paragraph_indices=indices,
    view='HTML text with pre/code line breaks; ordinary prose whitespace normalized',
    continuation='Use paragraph and zero-based start_character; partial windows are not complete executable blocks.')
links = _ManualLinks(document['url'], document['query'])
links.feed(html.decode('utf8'))
replay['manual_references'] = links.links
target.observations['document_excerpt'] = replay  # Memory only; old Result remains immutable.

# Reconstruct only the historical decision inputs and reservations, never save Run.
prior_records = [item for item in run.model_records if item['created_at'] < record['created_at']]
run.model_records = prior_records
run.usage.model_calls = len(prior_records)
run.usage.model_tokens_used = sum(item.get('total_tokens', 0) for item in prior_records)
run.usage.model_tokens_unknown = 0
run.usage.decision_rounds = len(prior_records)
run.calls = [call for call in run.calls if call.created_at < now]
run.result_ids = [result.id for result in results]
run.processed_feedback = [identifier for identifier in run.result_ids if identifier != RESULT]
run.state = 'ready'
run.control_generation = record['basis']['control_generation']
feedback = dict(old_context['DATA'].get('feedback', {}))
feedback['new_result_ids'] = [RESULT]
feedback['pending_step_ids'] = old_context['CONTROL'].get('pending_step_ids', [])
prepared = build_context(request, run, plan, results=results, feedback=feedback,
    relevant_tools=run.permission.allowed_tools, control_generation=run.control_generation,
    now=now, model_profile=record['model_profile'], delivery_snapshot=snapshot)
view = payload(prepared)
row = next(item for item in view['DATA']['results'] if item['result_id'] == RESULT)
visible = row['unqualified_observations']['document_excerpt']
assert any(line.lstrip().startswith('Nuclei =') for line in visible['excerpt'].splitlines())
assert prepared.input_token_bound <= run.budget.input_tokens
unchanged = all(hashlib.sha256(Path(path).read_bytes()).hexdigest() == digest for path, digest in tracked.items())
assert unchanged
output = {'scope': 'Offline counterfactual a025 call13: exact archived HTML, in-memory new excerpt and historical context; no network, model, Tool, ORCA or source record writes.',
    'prompt_version': PROMPT_VERSION,
    'original_model_id': MODEL, 'original_result_id': RESULT,
    'original_excerpt_characters': len(document['excerpt']), 'replayed_excerpt_characters': len(excerpt),
    'original_excerpt_had_nuclei_declaration': any(line.lstrip().startswith('Nuclei =') for line in document['excerpt'].splitlines()),
    'replayed_excerpt_has_nuclei_declaration': True,
    'original_context_result': next(row for row in old_context['DATA']['results'] if row['result_id'] == RESULT),
    'replayed_context_has_nuclei_declaration': True, 'windows': windows,
    'input_token_bound': prepared.input_token_bound, 'input_limit': run.budget.input_tokens,
    'original_input_reserved': record['input_reserved'],
    'source_hashes': tracked, 'originals_unchanged': unchanged,
    'production_sha256': {path: hashlib.sha256((ROOT / path).read_bytes()).hexdigest()
        for path in ['src/orca_agent/context.py', 'src/orca_agent/tools/knowledge.py']},
    'meaning': 'Documentation visibility only; excerpts remain partial and confer no scientific qualification or permission.'}
destination = ROOT / 'data/workflow-root-repair/a025-knowledge-window-replay-v89.json'
with destination.open('x', encoding='utf8') as stream:
    json.dump(output, stream, ensure_ascii=False, indent=2)
print(json.dumps({key: output[key] for key in ['input_token_bound', 'input_limit', 'replayed_context_has_nuclei_declaration', 'originals_unchanged']}))
