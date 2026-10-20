import json, os, re, csv
from orca_task_sdk import run_orca, receipt_source

REQUEST_KEY = 'tddft_b3lyp_def2tzvp'
receipt = run_orca('job.inp', dependencies=('acetone.xyz',), request_key=REQUEST_KEY, purpose='new_candidate')
files = receipt.get('files', {}) or {}
stdout_key = None
for key in files:
    if str(key).endswith('stdout.out') or os.path.basename(str(key)) == 'stdout.out':
        stdout_key = key
        break
if stdout_key is None:
    raise RuntimeError('ORCA receipt did not provide stdout.out; files keys: ' + repr(list(files.keys())))
with open(files[stdout_key], 'r', encoding='utf-8', errors='replace') as fh:
    text = fh.read()

def parse_states(text):
    states = []
    block_pat = re.compile(r'STATE\s+(\d+):\s+E=\s*([-+]?\d+(?:\.\d+)?)\s*au\s+([-+]?\d+(?:\.\d+)?)\s*eV\s+([-+]?\d+(?:\.\d+)?)\s*nm', re.I)
    for m in block_pat.finditer(text):
        tail = text[m.end():m.end()+2500]
        fm = re.search(r'fosc\s*=\s*([-+]?\d+(?:\.\d+)?(?:[Ee][-+]?\d+)?)', tail, re.I)
        if not fm:
            fm = re.search(r'\bf\s*=\s*([-+]?\d+(?:\.\d+)?(?:[Ee][-+]?\d+)?)', tail, re.I)
        fosc = float(fm.group(1)) if fm else None
        states.append({'state': int(m.group(1)), 'energy_eV': float(m.group(3)), 'wavelength_nm': float(m.group(4)), 'oscillator_strength_length_gauge': fosc})
    if states:
        return sorted(states, key=lambda x: x['state'])
    table_pat = re.compile(r'^\s*(\d+)\s+([-+]?\d+(?:\.\d+)?)\s+([-+]?\d+(?:\.\d+)?)\s+([-+]?\d+(?:\.\d+)?)\s*$', re.M)
    for m in table_pat.finditer(text):
        state = int(m.group(1))
        energy_ev = float(m.group(2))
        a = float(m.group(3))
        b = float(m.group(4))
        if a > 10.0:
            wavelength_nm = a
            fosc = b
        else:
            wavelength_nm = None
            fosc = a
        states.append({'state': state, 'energy_eV': energy_ev, 'wavelength_nm': wavelength_nm, 'oscillator_strength_length_gauge': fosc})
    return sorted(states, key=lambda x: x['state'])

states = parse_states(text)
if not states:
    for m in re.finditer(r'STATE\s+(\d+):\s+E=\s*([-+]?\d+(?:\.\d+)?)\s*au\s+([-+]?\d+(?:\.\d+)?)\s*eV', text, re.I):
        states.append({'state': int(m.group(1)), 'energy_eV': float(m.group(3)), 'wavelength_nm': None, 'oscillator_strength_length_gauge': None})
    states = sorted(states, key=lambda x: x['state'])
if not states:
    raise RuntimeError('Could not parse TDDFT states from stdout.out')
states = sorted(states, key=lambda x: (x['energy_eV'], x['state']))[:5]
states = sorted(states, key=lambda x: x['state'])
for s in states:
    if s['wavelength_nm'] is None and s['energy_eV'] > 0:
        s['wavelength_nm'] = 1239.841984 / s['energy_eV']
    if s['oscillator_strength_length_gauge'] is None:
        raise RuntimeError('Missing oscillator strength for state ' + str(s['state']))

with open('absorption.csv', 'w', newline='', encoding='utf-8') as fh:
    writer = csv.writer(fh)
    writer.writerow(['state', 'energy_eV', 'wavelength_nm', 'oscillator_strength_length_gauge'])
    for s in states:
        writer.writerow([s['state'], s['energy_eV'], s['wavelength_nm'], s['oscillator_strength_length_gauge']])

low = min(states, key=lambda s: s['energy_eV'])
weak = min(states, key=lambda s: s['oscillator_strength_length_gauge'])
strong = max(states, key=lambda s: s['oscillator_strength_length_gauge'])
method_choice = 'Full linear-response TDDFT was requested over TDA because it retains coupling between excitation and de-excitation amplitudes and is the more complete linear-response treatment for vertical singlet excitations. TDA is an approximation that neglects those coupling terms; it is cheaper but not chosen here. B3LYP/def2-TZVP is used within the frozen budget. This is a method-choice rationale, not an experimental validation.'
transition = 'Among the five lowest singlet vertical excitations, state ' + str(low['state']) + ' is the lowest-energy state (E=' + format(low['energy_eV'], '.6f') + ' eV, fosc=' + format(low['oscillator_strength_length_gauge'], '.6f') + '). The weakest state is ' + str(weak['state']) + ' (fosc=' + format(weak['oscillator_strength_length_gauge'], '.6f') + ') and the strongest is ' + str(strong['state']) + ' (fosc=' + format(strong['oscillator_strength_length_gauge'], '.6f') + '). The low-energy weak feature is consistent with a weakly allowed or symmetry-forbidden n->pi* transition; the stronger feature is consistent with a more allowed pi->pi* transition. These are vertical TDDFT results, not experimental absorption maxima or adiabatic excitation energies.'
delivery = {'geometry': 'acetone.xyz', 'input': 'job.inp', 'raw_response': 'stdout.out from ORCA receipt', 'absorption_table': 'absorption.csv', 'report': 'report.md', 'script': 'analyze_tddft.py', 'data_table': 'absorption.csv', 'random_seed': 1701, 'limitations': 'Initial prepared geometry, not optimized; vertical TDDFT values are not experimental peaks.'}

with open('report.md', 'w', encoding='utf-8') as fh:
    fh.write('''# Acetone TDDFT vertical excitations

Geometry: initial prepared geometry `acetone.xyz` from structure.prepare; not optimized.

Conditions: gas phase, charge 0, multiplicity 1, RKS, B3LYP/def2-TZVP, full linear-response TDDFT (TDA false), 5 roots.

Method choice: ''')
    fh.write(method_choice)
    fh.write('''

| State | Energy (eV) | Wavelength (nm) | fosc (length gauge) |
|---:|---:|---:|---:|
''')
    for s in states:
        fh.write('| ' + str(s['state']) + ' | ' + format(s['energy_eV'], '.6f') + ' | ' + format(s['wavelength_nm'], '.3f') + ' | ' + format(s['oscillator_strength_length_gauge'], '.6f') + ' |' + os.linesep)
    fh.write('''
''')
    fh.write(transition)
    fh.write('''

Limitations: The geometry is an initial prepared structure, not a relaxed minimum. Vertical excitation energies are not experimental absorption peaks or adiabatic excitation energies. def2-TZVP lacks diffuse augmentation, so diffuse/Rydberg states may be less well described.

Random seed: 1701. No stochastic search or structure-generation algorithm was used in this managed ORCA calculation.
''')

stdout_src = receipt_source(receipt, stdout_key, location='TDDFT excited states section')
job_src = {'filename': 'job.inp', 'location': 'full input'}
geom_src = {'filename': 'acetone.xyz', 'location': 'prepared geometry used'}
observations = [
    {'name': 'method_choice_explanation', 'value': method_choice, 'unit': None, 'sources': [job_src, stdout_src], 'formula': None, 'artifacts': ['report.md'], 'limitations': ['Method-choice rationale, not an experimental validation.']},
    {'name': 'vertical_excitations', 'value': states, 'unit': 'eV (energy); nm (wavelength); dimensionless (oscillator strength)', 'sources': [stdout_src], 'formula': 'Vertical excitation energies and length-gauge oscillator strengths as printed by ORCA TDDFT', 'artifacts': ['absorption.csv'], 'limitations': ['Vertical TDDFT results; not experimental absorption peaks or adiabatic energies.']},
    {'name': 'excitation_table', 'value': states, 'unit': 'eV (energy); nm (wavelength); dimensionless (oscillator strength)', 'sources': [stdout_src], 'formula': 'Same parsed TDDFT table, preserving all five states including weak/zero oscillator strengths', 'artifacts': ['absorption.csv'], 'limitations': ['Values are printed by ORCA and parsed; no experimental correction.']},
    {'name': 'transition_analysis', 'value': transition, 'unit': None, 'sources': [stdout_src, job_src], 'formula': 'Comparison of printed fosc values; no fitting', 'artifacts': ['report.md'], 'limitations': ['Qualitative assignment based on oscillator strengths; no experimental spectra.']},
    {'name': 'delivery_contents', 'value': delivery, 'unit': None, 'sources': [job_src, geom_src, stdout_src], 'formula': None, 'artifacts': ['absorption.csv', 'report.md'], 'limitations': ['Initial geometry; vertical TDDFT limitations as stated.']},
    {'name': 'random_seed', 'value': 1701, 'unit': None, 'sources': [], 'formula': None, 'artifacts': ['report.md'], 'limitations': ['No random-number-consuming search was used; seed recorded for reproducibility.']}
]
with open('observations.json', 'w', encoding='utf-8') as fh:
    json.dump({'version': 1, 'observations': observations}, fh, indent=2, allow_nan=False)
print('wrote observations.json and artifacts')
