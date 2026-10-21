import json
from orca_task_sdk import run_orca, receipt_source

def read_text(path):
    with open(path, 'r', encoding='utf-8', errors='replace') as f:
        return f.read()

def finite(x):
    try:
        v = float(x)
        if v != v or v in (float('inf'), float('-inf')):
            return None
        return v
    except Exception:
        return None

def parse_xyz(text):
    lines = text.splitlines()
    start = 0
    if lines and lines[0].strip().isdigit():
        start = 2
    atoms = []
    for line in lines[start:]:
        parts = line.split()
        if len(parts) >= 4:
            x = finite(parts[1])
            y = finite(parts[2])
            z = finite(parts[3])
            if x is not None and y is not None and z is not None:
                atoms.append({'element': parts[0], 'x': x, 'y': y, 'z': z})
    return atoms

def parse_freqs(text):
    out = {}
    for line in text.splitlines():
        s = line.strip()
        if 'cm**-1' not in s and 'cm^-1' not in s:
            continue
        if ':' not in s:
            continue
        left, right = s.split(':', 1)
        try:
            idx = int(left.strip())
        except Exception:
            continue
        token = None
        for part in right.replace('=', ' ').split():
            v = finite(part)
            if v is not None:
                token = v
                break
        if token is not None and idx not in out:
            out[idx] = token
    return [{'mode': i, 'frequency_cm-1': out[i]} for i in sorted(out)]

def convergence_markers(text):
    markers = []
    for line in text.splitlines():
        low = line.lower()
        if 'optimization has converged' in low or 'hurray' in low or 'convergence achieved' in low:
            markers.append(line.strip())
    return markers

initial_text = read_text('coords.xyz')
initial_atoms = parse_xyz(initial_text)

orca_input = '''! RKS r2SCAN-3c Opt Freq NoAutoStart
%maxcore 192
%scf
  MaxIter 256
  AutoTRAH false
end
%geom
  MaxIter 128
end
* xyzfile 0 1 coords.xyz
'''
with open('opt_freq.inp', 'w', encoding='utf-8') as f:
    f.write(orca_input)

receipt = None
error_text = None
try:
    receipt = run_orca('opt_freq.inp', dependencies=['coords.xyz'], request_key='opt_freq', purpose='new_candidate')
except Exception as e:
    error_text = repr(e)

files = {}
if isinstance(receipt, dict):
    files = receipt.get('files', {}) or {}

stdout_text = ''
if 'stdout.out' in files:
    try:
        stdout_text = read_text(files['stdout.out'])
    except Exception as e:
        error_text = (error_text + '; ' if error_text else '') + repr(e)

markers = convergence_markers(stdout_text)
converged = len(markers) > 0

final_atoms = []
if 'job.xyz' in files:
    try:
        final_atoms = parse_xyz(read_text(files['job.xyz']))
    except Exception as e:
        error_text = (error_text + '; ' if error_text else '') + repr(e)

freqs = parse_freqs(stdout_text)

retained = []
for key in ['stdout.out', 'stderr.txt', 'job.xyz', 'job.hess', 'job.inp']:
    if key in files:
        try:
            with open(files[key], 'rb') as f:
                data = f.read()
            outname = 'retained_' + key.replace('/', '_')
            with open(outname, 'wb') as f:
                f.write(data)
            retained.append(outname)
        except Exception as e:
            error_text = (error_text + '; ' if error_text else '') + repr(e)

def src(filename, location):
    if receipt is not None and filename in files:
        return [receipt_source(receipt, filename, location=location)]
    return []

limitations = []
if len(freqs) != 27:
    limitations.append('Parsed %d frequency lines; expected 27.' % len(freqs))
if not final_atoms:
    limitations.append('No final geometry parsed from job.xyz.')

obs_initial = {
    'name': 'initial_geometry',
    'value': {'atoms': initial_atoms, 'charge': 0, 'multiplicity': 1, 'note': 'Initial prepared geometry; not optimized.'},
    'unit': 'Å',
    'sources': [{'filename': 'coords.xyz', 'location': 'input manifest prepared_geometry'}],
    'formula': None,
    'artifacts': ['coords.xyz'],
    'limitations': ['Initial geometry is not an optimized structure.'] if initial_atoms else ['No initial atoms parsed.']
}

obs_opt = {
    'name': 'geometry_optimization',
    'value': {'optimization_convergence_marker_found': converged, 'convergence_markers': markers[:5], 'note': 'Normal termination is not treated as convergence.'},
    'unit': None,
    'sources': src('stdout.out', 'optimization output'),
    'formula': None,
    'artifacts': retained,
    'limitations': ['Convergence is judged only by explicit marker text; ORCA output was not independently verified.'] if not converged else []
}

obs_atoms = {
    'name': 'atomic_coordinates',
    'value': {'atoms': final_atoms, 'note': 'Final geometry from job.xyz; coordinates in Å.'},
    'unit': 'Å',
    'sources': src('job.xyz', 'final optimized geometry'),
    'formula': None,
    'artifacts': ['retained_job.xyz'] if 'retained_job.xyz' in retained else [],
    'limitations': ['Coordinates are not independently verified.'] if final_atoms else ['No final coordinates parsed.']
}

freq_sources = src('stdout.out', 'VIBRATIONAL FREQUENCIES') + src('job.hess', 'Hessian')
obs_freq = {
    'name': 'vibrational_frequencies',
    'value': {'modes': freqs, 'count': len(freqs)},
    'unit': 'cm^-1',
    'sources': freq_sources,
    'formula': None,
    'artifacts': [x for x in ['retained_stdout.out', 'retained_job.hess'] if x in retained],
    'limitations': limitations
}

vib_modes = freqs[6:] if len(freqs) >= 27 else []
neg = [m for m in vib_modes if m['frequency_cm-1'] < 0]
supports = converged and len(freqs) == 27 and len(neg) == 0
obs_local = {
    'name': 'local_minimum_assessment',
    'value': {'supports_local_minimum_within_harmonic_model': supports, 'optimization_convergence_marker_found': converged, 'frequency_count': len(freqs), 'negative_vibrational_modes': neg, 'note': 'Limited to harmonic frequency result for this optimized structure; no global minimum claim.'},
    'unit': None,
    'sources': src('stdout.out', 'frequency and optimization output'),
    'formula': None,
    'artifacts': [x for x in ['retained_stdout.out'] if x in retained],
    'limitations': ['Harmonic approximation and finite single-structure assessment only.']
}

obs_retention = {
    'name': 'artifact_retention',
    'value': {'input_file': 'opt_freq.inp', 'script_file': 'run_ethanol_orca.py', 'retained_files': retained, 'receipt_file_keys': sorted(files.keys()), 'note': 'Raw files referenced; failure text retained in this observation.'},
    'unit': None,
    'sources': [],
    'formula': None,
    'artifacts': ['opt_freq.inp'] + retained,
    'limitations': [] if receipt is not None else ['No ORCA receipt was produced.']
}

gaps = []
if receipt is None:
    gaps.append('ORCA execution failed or returned no receipt: ' + str(error_text))
if not converged:
    gaps.append('No explicit ORCA optimization convergence marker found.')
if len(freqs) != 27:
    gaps.append('Parsed %d frequency lines; expected 27.' % len(freqs))
if not supports:
    gaps.append('Frequency result does not support a local minimum under the stated harmonic assessment.')
gaps.append('Scientific validation is reported with limitations; ORCA output was not independently re-verified.')
gaps.append('No global minimum or conformer search was performed.')
gaps.append('Frequency and local-minimum conclusion are limited to the harmonic approximation and selected r2SCAN-3c method.')
obs_gaps = {
    'name': 'verification_gaps',
    'value': {'gaps': gaps, 'execution_error': error_text},
    'unit': None,
    'sources': [],
    'formula': None,
    'artifacts': retained,
    'limitations': []
}

obs_seed = {
    'name': 'random_seed_record',
    'value': {'seed': 1701, 'random_numbers_used_by_task_workflow': False, 'note': 'No random-number generation is used in the task-authored ORCA workflow. Seed 1701 is recorded for reproducibility if future stochastic steps are added; fixed-seed support is not applicable to this deterministic workflow.'},
    'unit': None,
    'sources': [],
    'formula': None,
    'artifacts': [],
    'limitations': []
}

observations = {'version': 1, 'observations': [obs_initial, obs_opt, obs_atoms, obs_freq, obs_local, obs_retention, obs_gaps, obs_seed]}
with open('observations.json', 'w', encoding='utf-8') as f:
    json.dump(observations, f, ensure_ascii=False, indent=2)
print(json.dumps(observations, ensure_ascii=False))
