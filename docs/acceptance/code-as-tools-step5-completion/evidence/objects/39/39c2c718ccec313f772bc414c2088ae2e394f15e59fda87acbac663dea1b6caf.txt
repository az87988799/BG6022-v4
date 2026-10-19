"""One natural-language development run; this driver contains no scientific solver."""
import hashlib
import json
import subprocess
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from orca_agent.config import Config
from orca_agent.entrypoints import create_text, execute
from orca_agent.models import utc_now
from orca_agent.report import build_report, render_report
from orca_agent.store import Store, atomic_write
from scripts.code_tools_development import snapshot

root = Path.cwd()
batch = root / 'data/code-as-tools-step5'
evidence = batch / 'live-006'
evidence.mkdir(exist_ok=False)
config = Config.model_validate_json((batch / 'config-final-v65.json').read_bytes())
text = (batch / 'request-english.txt').read_text(encoding='utf-8')
frozen = {'created_at': utc_now().isoformat(), 'git_head': subprocess.check_output(['git','rev-parse','HEAD'], text=True).strip(),
          'source_files': snapshot(root), 'configuration': config.model_dump(mode='json'), 'text': text,
          'scope': 'step5 one natural-language development run; no injected Plan/input/script',
          'driver_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
          'limits': {'runs': 6, 'model_calls': 72, 'model_tokens': 3600000, 'orca_starts': 12}}
def write(name, value):
    atomic_write(evidence / name, (json.dumps(value, ensure_ascii=False, indent=2)+'\n').encode(), immutable=True)
write('freeze.json', frozen)
store = Store(config.data_root, configuration=config.run_configuration())
run, _ = create_text(store, config, text)
write('run-location.json', {'run_id': run.id, 'data_root': str(store.root), 'environment_root': str(store.environment_root)})
print(json.dumps({'run_id': run.id, 'evidence': str(evidence)}), flush=True)
error = None
try:
    run = execute(store, config, run.id)
except Exception as exc:
    error = {'type': type(exc).__name__, 'message': str(exc)}
    run = store.load_run(run.id)
changed = sorted(key for key, value in snapshot(root).items() if frozen['source_files'].get(key) != value)
report = build_report(store, run)
write('report.json', report)
atomic_write(evidence / 'report.txt', render_report(report).encode('utf-8'), immutable=True)
write('outcome.json', {'state': run.state, 'goals': run.goal_status, 'delivery_status': run.delivery_status,
     'usage': run.usage.model_dump(mode='json'), 'error': error, 'source_changed_during_run': changed,
     'finished_at': utc_now().isoformat()})
print(json.dumps({'state': run.state, 'goals': run.goal_status, 'error': error, 'changed': changed}), flush=True)
