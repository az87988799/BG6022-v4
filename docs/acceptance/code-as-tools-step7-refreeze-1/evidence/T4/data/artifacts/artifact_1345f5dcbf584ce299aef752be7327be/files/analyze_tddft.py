import json
import re

GAUGE_HEADER = re.compile(r'ABSORPTION SPECTRUM VIA TRANSITION ELECTRIC DIPOLE MOMENTS', re.I)
ROW = re.compile(r'^\s*(\d+)\s+([-+]?\d+(?:\.\d+)?)\s+([-+]?\d+(?:\.\d+)?)\s+([-+]?\d+(?:\.\d+)?)')
STATE_RE = re.compile(r'^\s*STATE\s+(\d+)\s*:\s*E=\s*([-+]?\d+(?:\.\d+)?)\s*au\s+([-+]?\d+(?:\.\d+)?)\s*eV', re.I)
VEC_RE = re.compile(r'^\s*([-+]?\d+\.\d+)\s+([-+]?\d+\.\d+)\s+([-+]?\d+\.\d+)')
CM1_PER_EV = 8065.544
NM_EV = 1239.841984


def read_lines(name):
    try:
        with open(name, 'r', errors='replace') as fh:
            return fh.read().splitlines()
    except OSError:
        return []


def parse_table(lines):
    for i, line in enumerate(lines):
        if not GAUGE_HEADER.search(line):
            continue
        rows = []
        header_seen = False
        for s in lines[i + 1:i + 600]:
            if re.match(r'^\s*Trans\.', s, re.I):
                header_seen = True
                continue
            m = ROW.match(s)
            if header_seen and m:
                rows.append({'state_index': int(m.group(1)),
                             'energy_cm1': float(m.group(2)),
                             'wavelength_nm': float(m.group(3)),
                             'fosc': float(m.group(4))})
            elif rows:
                break
        if rows:
            return rows
    return []


def parse_states(lines):
    found = []
    for i, line in enumerate(lines):
        m = STATE_RE.match(line)
        if not m:
            continue
        vec = None
        for s in lines[i + 1:i + 6]:
            mm = VEC_RE.match(s)
            if mm:
                vec = [float(mm.group(1)), float(mm.group(2)), float(mm.group(3))]
                break
        if vec is None:
            continue
        e_au = float(m.group(2))
        found.append({'state_index': int(m.group(1)),
                      'energy_eV': float(m.group(3)),
                      'fosc': (2.0 / 3.0) * e_au * sum(v * v for v in vec)})
    return found


cis = read_lines('job.cis')
stdout = read_lines('stdout.out')
inp_lines = read_lines('job.inp')
xyz_lines = read_lines('coords.xyz')

rows = parse_table(cis)
origin = 'job.cis'
if not rows:
    rows = parse_table(stdout)
    origin = 'stdout.out'

excitations = []
if rows:
    for r in sorted(rows, key=lambda d: (d['energy_cm1'], d['state_index'])):
        excitations.append({'state_index': r['state_index'],
                            'energy_eV': r['energy_cm1'] / CM1_PER_EV,
                            'energy_cm1': r['energy_cm1'],
                            'wavelength_nm': r['wavelength_nm'],
                            'fosc_length_gauge': r['fosc']})
else:
    st = parse_states(cis)
    origin = 'job.cis'
    if not st:
        st = parse_states(stdout)
        origin = 'stdout.out'
    for r in sorted(st, key=lambda d: (d['energy_eV'], d['state_index'])):
        excitations.append({'state_index': r['state_index'],
                            'energy_eV': r['energy_eV'],
                            'energy_cm1': r['energy_eV'] * CM1_PER_EV,
                            'wavelength_nm': (NM_EV / r['energy_eV']) if r['energy_eV'] else None,
                            'fosc_length_gauge': r['fosc']})

if not excitations:
    raise SystemExit('no excitation rows parsed from job.cis or stdout.out')

lowest5 = excitations[:5]
energies = [round(e['energy_eV'], 6) for e in lowest5]
foscs = [round(e['fosc_length_gauge'], 8) for e in lowest5]

strong = max(excitations, key=lambda d: d['fosc_length_gauge'])
weak = min(excitations, key=lambda d: d['fosc_length_gauge'])

with open('absorption_table.csv', 'w') as fh:
    fh.write('state_index,energy_eV,energy_cm-1,wavelength_nm,fosc_length_gauge\n')
    for e in excitations:
        wl = '' if e['wavelength_nm'] is None else ('%.3f' % e['wavelength_nm'])
        fh.write('%d,%.6f,%.3f,%s,%.8f\n' % (e['state_index'], e['energy_eV'], e['energy_cm1'], wl, e['fosc_length_gauge']))

method_line = ''
tddft_lines = []
in_tddft = False
for s in inp_lines:
    t = s.strip()
    if not method_line and t.startswith('!'):
        method_line = t
    if t.lower().startswith('%tddft'):
        in_tddft = True
    if in_tddft:
        tddft_lines.append(t)
        if t.lower().endswith('end'):
            in_tddft = False

natoms = 0
elements = []
for s in xyz_lines:
    parts = s.split()
    if len(parts) == 4:
        natoms += 1
        elements.append(parts[0])

seed_value = 'unknown'
ident = {}
try:
    with open('identity.json', 'r') as fh:
        ident = json.load(fh)
except Exception:
    ident = {}
if isinstance(ident, dict):
    if 'random_seed' in ident:
        seed_value = repr(ident.get('random_seed'))
    else:
        for v in ident.values():
            if isinstance(v, dict) and 'random_seed' in v:
                seed_value = repr(v.get('random_seed'))
                break

exc_sources = [{'filename': origin, 'location': ('ABSORPTION SPECTRUM VIA TRANSITION ELECTRIC DIPOLE MOMENTS table' if rows else 'per-STATE energies and length-gauge transition dipoles')}]

analysis = ('Parsed %d roots from %s ordered by vertical excitation energy. Lowest five: ' % (len(excitations), origin)) + '; '.join(
    'state %d: %.3f eV, f(length)=%.5f' % (e['state_index'], e['energy_eV'], e['fosc_length_gauge']) for e in lowest5) + (
    '. Strongest parsed root: state %d at %.3f eV with f(length)=%.5f; weakest: state %d at %.3f eV with f(length)=%.5f. '
    'The low-energy roots carry small dipole-length oscillator strength while the stronger roots at higher energy carry much '
    'larger values, so the low-energy and strong-transition regimes differ by computed transition intensity, not by any '
    'orbital-energy gap. These are vertical TDDFT response values at one fixed geometry, not experimental absorption maxima '
    'and not adiabatic excitation energies. No configurational assignment (for example n->pi* labels) is asserted here, '
    'because the parsed oscillator strengths alone do not establish state character.'
    % (strong['state_index'], strong['energy_eV'], strong['fosc_length_gauge'],
       weak['state_index'], weak['energy_eV'], weak['fosc_length_gauge']))

rationale = ('Managed ORCA input method line: %s ; TDDFT response block: %s . '
             'PBE0 (global hybrid, 25%% HF exchange) was selected for a bounded linear-response TDDFT treatment of valence '
             'singlet excitations of acetone, with the def2-SVP polarized valence double-zeta basis. Full TDDFT (not TDA) with '
             'NRoots 5 and singlets only was requested at the single prepared geometry under gas-phase (no solvent) conditions. '
             'Method and basis selection is a planning choice and is not independent scientific validation; the response block '
             'above records what was actually requested.'
             % (method_line, ' | '.join(tddft_lines)))

geometry_provenance = ('The geometry consumed by the response calculation is the task-prepared XYZ coords.xyz (%d atoms, '
                       'elements %s) produced by structure.resolve from SMILES CC(=O)C with generator '
                       'opi.Structure.from_smiles / RDKit. Its recorded guarantee covers only initial graph/atom mapping and finite '
                       'coordinates, not optimization or ground-state certification. No geometry optimization was performed in this '
                       'workflow, so the reported values are vertical excitations at a non-optimized structure and no frequency '
                       'validation of a minimum is available.'
                       % (natoms, ','.join(elements)))

seed_policy = ('Structure preparation used opi.Structure.from_smiles; the recorded identity artifact reports '
               'random_seed=%s and generation_reproducibility="no seed exposed; reuse this immutable XYZ". A fixed seed 1701 '
               'could not be applied to that generator, so this is recorded as not fixable rather than claimed as deterministic. '
               'The ORCA TDDFT response itself is deterministic for a fixed input and geometry (no stochastic sampling), and the '
               'parsing/tabulation step used no random numbers.' % seed_value)

observations = [
    {'name': 'vertical_excitations', 'value': lowest5, 'unit': None,
     'sources': exc_sources,
     'formula': 'E(eV) = E(cm-1)/8065.544 for the electric-dipole (length gauge) table; oscillator strengths taken directly from the printed dipole-length fosc column',
     'artifacts': ['absorption_table.csv'],
     'limitations': ['lowest five singlet vertical excitations; all parsed roots are retained in absorption_table.csv',
                     'vertical response values at one fixed geometry, not experimental absorption maxima and not adiabatic excitation energies',
                     'def2-SVP has no diffuse functions, so Rydberg states are not targeted']},
    {'name': 'vertical_excitation_energies_eV', 'value': energies, 'unit': 'eV',
     'sources': exc_sources, 'formula': 'E(cm-1)/8065.544', 'artifacts': ['absorption_table.csv'], 'limitations': []},
    {'name': 'vertical_excitation_oscillator_strengths', 'value': foscs, 'unit': 'dimensionless',
     'sources': exc_sources, 'formula': 'printed ORCA electric-dipole (length gauge) oscillator strength fosc',
     'artifacts': ['absorption_table.csv'], 'limitations': []},
    {'name': 'absorption_table', 'value': excitations, 'unit': None, 'sources': exc_sources, 'formula': None,
     'artifacts': ['absorption_table.csv'],
     'limitations': ['full parsed sequence retained, including weak and near-zero oscillator strengths; no filtering applied']},
    {'name': 'excitation_analysis', 'value': analysis, 'unit': None, 'sources': exc_sources, 'formula': None,
     'artifacts': ['absorption_table.csv'], 'limitations': []},
    {'name': 'method_selection_rationale', 'value': rationale, 'unit': None,
     'sources': [{'filename': 'job.inp', 'location': 'method line and %tddft block'}], 'formula': None, 'artifacts': [], 'limitations': []},
    {'name': 'geometry_provenance', 'value': geometry_provenance, 'unit': None,
     'sources': [{'filename': 'coords.xyz', 'location': 'atom rows'},
                 {'filename': 'identity.json', 'location': 'generator and guarantee fields'}],
     'formula': None, 'artifacts': [], 'limitations': []},
    {'name': 'random_seed_policy', 'value': seed_policy, 'unit': None,
     'sources': [{'filename': 'identity.json', 'location': 'random_seed and generation_reproducibility'}],
     'formula': None, 'artifacts': [], 'limitations': []},
    {'name': 'delivery_bundle',
     'value': ['coords.xyz', 'job.inp', 'job.cis', 'stdout.out', 'absorption_table.csv', 'observations.json', 'analyze_tddft.py'],
     'unit': None, 'sources': [{'filename': 'job.inp', 'location': None}], 'formula': None,
     'artifacts': ['absorption_table.csv', 'observations.json'], 'limitations': []},
]

with open('observations.json', 'w') as fh:
    json.dump({'version': 1, 'observations': observations}, fh, indent=2)

print(json.dumps({'origin': origin, 'parsed_roots': len(excitations), 'lowest5_eV': energies,
                  'lowest5_fosc': foscs, 'seed': seed_value}, indent=2))
