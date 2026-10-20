import json
import math
import time
from pathlib import Path

from orca_task_sdk import run_orca

try:
    from orca_task_sdk import receipt_source as _receipt_source
except Exception:
    _receipt_source = None

GEOM = 'butane_initial.xyz'
ANGLES = [-180.0, -120.0, -60.0, 0.0, 60.0, 120.0, 180.0]
HARTREE_TO_KJ = 2625.4996394799
TIME_BUDGET_S = 950.0
START = time.monotonic()
CRITERION = (
    'Pre-declared refinement criterion (stated before the coarse scan): fit a parabola through '
    'every triplet of three consecutive coarse grid points (60 deg spacing) whose middle point is '
    'a local extremum of the electronic energy. Add a refinement point (at most 2, separated by at '
    'least 15 deg, inside -180..180) only if for such a triplet (1) the parabola vertex lies more '
    'than 12 deg from the nearest computed grid point, (2) the parabola predicts more than 0.5 '
    'kJ/mol lowering relative to the middle computed point, and (3) the predicted vertex lies '
    'inside the scanned interval. Otherwise do not add points. The continue/stop decision itself '
    'is taken in the main decision loop after these coarse observations are returned.'
)


def read_xyz_text(text):
    lines = text.splitlines()
    nat = int(lines[0].split()[0])
    atoms = []
    for line in lines[2:2 + nat]:
        parts = line.split()
        atoms.append((parts[0], float(parts[1]), float(parts[2]), float(parts[3])))
    return atoms


def distance(a, b):
    return math.sqrt((a[1] - b[1]) ** 2 + (a[2] - b[2]) ** 2 + (a[3] - b[3]) ** 2)


def dihedral(atoms, i, j, k, l):
    p0 = atoms[i][1:]
    p1 = atoms[j][1:]
    p2 = atoms[k][1:]
    p3 = atoms[l][1:]
    b0 = [p0[m] - p1[m] for m in range(3)]
    b1 = [p2[m] - p1[m] for m in range(3)]
    b2 = [p3[m] - p2[m] for m in range(3)]

    def cross(u, v):
        return [u[1] * v[2] - u[2] * v[1], u[2] * v[0] - u[0] * v[2], u[0] * v[1] - u[1] * v[0]]

    def dot(u, v):
        return u[0] * v[0] + u[1] * v[1] + u[2] * v[2]

    n1 = cross(b0, b1)
    n2 = cross(b1, b2)
    nb1 = math.sqrt(dot(b1, b1))
    if nb1 < 1e-12:
        raise ValueError('degenerate dihedral geometry')
    m1 = cross(n1, [x / nb1 for x in b1])
    return math.degrees(math.atan2(dot(m1, n2), dot(n1, n2)))


def carbon_chain(atoms):
    cs = [i for i, a in enumerate(atoms) if a[0].capitalize() == 'C']
    adj = {i: [] for i in cs}
    for x in range(len(cs)):
        for y in range(x + 1, len(cs)):
            i = cs[x]
            j = cs[y]
            if distance(atoms[i], atoms[j]) < 1.75:
                adj[i].append(j)
                adj[j].append(i)
    ends = sorted(i for i in cs if len(adj[i]) == 1)
    if not ends:
        raise ValueError('no terminal carbon found; central chain undefined')
    path = [ends[0]]
    prev = None
    cur = ends[0]
    while True:
        nxt = [k for k in adj[cur] if k != prev]
        if not nxt:
            break
        if len(nxt) > 1:
            raise ValueError('branched carbon skeleton; central chain ambiguous')
        prev = cur
        cur = nxt[0]
        path.append(cur)
    return path


def build_input(chain, angle):
    i = chain[0]
    j = chain[1]
    k = chain[2]
    l = chain[3]
    constraint = f'    {{D {i} {j} {k} {l} {angle:.1f} C}}'
    lines = [
        '! r2SCAN-3c RKS Opt NoAutoStart',
        '%maxcore 192',
        '%scf',
        '  MaxIter 256',
        '  AutoTRAH false',
        'end',
        '%geom',
        '  MaxIter 128',
        '  Constraints',
        constraint,
        '  end',
        'end',
        '* xyzfile 0 1 ' + GEOM,
        '',
    ]
    return '\n'.join(lines)


def parse_energy(text):
    val = None
    for line in text.splitlines():
        if 'FINAL SINGLE POINT ENERGY' in line:
            parts = line.split()
            try:
                val = float(parts[-1])
            except (IndexError, ValueError):
                pass
    return val


def parse_converged(text):
    return 'THE OPTIMIZATION HAS CONVERGED' in text.upper()


def last_xyz_from_text(text):
    marker = 'CARTESIAN COORDINATES (ANGSTROEM)'
    idx = text.rfind(marker)
    if idx < 0:
        return None
    rows = []
    started = False
    for line in text[idx + len(marker):].splitlines():
        parts = line.split()
        if len(parts) >= 4:
            try:
                rows.append((parts[0], float(parts[1]), float(parts[2]), float(parts[3])))
                started = True
                continue
            except ValueError:
                if started:
                    break
                continue
        elif started:
            break
    if not rows:
        return None
    body = '\n'.join(f'{a:2s} {x:16.8f} {y:16.8f} {z:16.8f}' for a, x, y, z in rows)
    return f'{len(rows)}\nfinal geometry parsed from stdout\n{body}\n'


def src(rec, filename, location):
    if _receipt_source is not None:
        try:
            return _receipt_source(rec, filename, location=location)
        except Exception:
            pass
    return {'internal_request_key': rec.get('request_key'), 'filename': filename, 'location': location}


initial_text = Path(GEOM).read_text(encoding='utf-8', errors='replace')
initial_atoms = read_xyz_text(initial_text)
chain = carbon_chain(initial_atoms)
if len(chain) < 4:
    raise ValueError('central carbon chain shorter than four atoms')
CONS = [chain[0], chain[1], chain[2], chain[3]]

members = []
failed = []
records = {}

for angle in ANGLES:
    label = f'{int(angle):+04d}'
    key = 'scan_dih_' + label.replace('-', 'm').replace('+', 'p')
    inp_name = f'scan{label}.inp'
    Path(inp_name).write_text(build_input(CONS, angle), encoding='utf-8')
    if time.monotonic() - START > TIME_BUDGET_S:
        failed.append({'target_angle_deg': angle, 'request_key': key, 'stage': 'not_attempted',
                       'error': 'frozen script time budget exhausted before launching this point'})
        Path('scan_results.json').write_text(json.dumps({'members': members, 'failed': failed}, indent=2), encoding='utf-8')
        continue
    try:
        rec = run_orca(inp_name, dependencies=[GEOM], request_key=key, purpose='new_candidate')
    except Exception as exc:
        failed.append({'target_angle_deg': angle, 'request_key': key, 'stage': 'run_orca',
                       'error': type(exc).__name__ + ': ' + str(exc)})
        Path('scan_results.json').write_text(json.dumps({'members': members, 'failed': failed}, indent=2), encoding='utf-8')
        continue
    records[key] = rec
    files = rec.get('files') or {}
    text = ''
    out_path = files.get('stdout.out')
    if out_path:
        try:
            if Path(out_path).exists():
                text = Path(out_path).read_text(encoding='utf-8', errors='replace')
        except Exception:
            text = ''
    final_xyz = None
    xyz_path = files.get('job.xyz')
    if xyz_path:
        try:
            if Path(xyz_path).exists():
                final_xyz = Path(xyz_path).read_text(encoding='utf-8', errors='replace')
        except Exception:
            final_xyz = None
    if final_xyz is None:
        final_xyz = last_xyz_from_text(text)
    energy = parse_energy(text)
    converged = parse_converged(text)
    actual = None
    natoms = None
    member_xyz_name = None
    if final_xyz:
        member_xyz_name = f'member{label}.xyz'
        Path(member_xyz_name).write_text(final_xyz, encoding='utf-8')
        try:
            fatoms = read_xyz_text(final_xyz)
            natoms = len(fatoms)
            actual = dihedral(fatoms, CONS[0], CONS[1], CONS[2], CONS[3])
        except Exception:
            actual = None
    ok = energy is not None and converged and actual is not None
    members.append({
        'target_angle_deg': angle,
        'actual_dihedral_deg': actual,
        'energy_hartree': energy,
        'optimization_converged': bool(converged),
        'operation_status': rec.get('operation_status'),
        'request_key': key,
        'input_file': inp_name,
        'final_geometry_file': member_xyz_name,
        'atom_count': natoms,
        'status': 'completed' if ok else 'incomplete',
    })
    if not ok:
        failed.append({'target_angle_deg': angle, 'request_key': key, 'stage': 'postprocess',
                       'operation_status': rec.get('operation_status'),
                       'error': 'energy, convergence flag or final geometry missing/unparsable'})
    Path('scan_results.json').write_text(json.dumps({'members': members, 'failed': failed}, indent=2), encoding='utf-8')

good = [m for m in members if m.get('energy_hartree') is not None]
ordered = sorted(good, key=lambda x: x['target_angle_deg'])
emin = min(m['energy_hartree'] for m in good) if good else None
rel = []
for m in ordered:
    delta = None
    if emin is not None:
        delta = (m['energy_hartree'] - emin) * HARTREE_TO_KJ
    rel.append({'target_angle_deg': m['target_angle_deg'],
                'actual_dihedral_deg': m['actual_dihedral_deg'],
                'energy_hartree': m['energy_hartree'],
                'relative_energy_kj_mol': delta})

pts = sorted((m['target_angle_deg'], m['energy_hartree']) for m in good)
evaluation = []
recommended = []
for t in range(1, len(pts) - 1):
    x1, y1 = pts[t - 1]
    x2, y2 = pts[t]
    x3, y3 = pts[t + 1]
    h = x2 - x1
    if abs((x3 - x2) - h) > 1e-6:
        continue
    b = (y3 - y1) / 2.0
    a = (y1 + y3) / 2.0 - y2
    entry = {'triplet_angles_deg': [x1, x2, x3],
             'middle_is_local_min': y2 < y1 and y2 < y3,
             'middle_is_local_max': y2 > y1 and y2 > y3}
    if abs(a) > 1e-16:
        tstar = -b / (2.0 * a)
        offset = tstar * h
        lowering = (b * b) / (4.0 * a) * HARTREE_TO_KJ
        vertex_angle = x2 + offset
        ext = entry['middle_is_local_min'] or entry['middle_is_local_max']
        met = bool(ext and abs(offset) > 12.0 and lowering > 0.5 and -180.0 < vertex_angle < 180.0)
        entry['vertex_offset_from_middle_deg'] = offset
        entry['predicted_lowering_kj_mol'] = lowering
        entry['criterion_met'] = met
        entry['candidate_angle_deg'] = vertex_angle
        if met:
            recommended.append(vertex_angle)
    else:
        entry['vertex_offset_from_middle_deg'] = None
        entry['predicted_lowering_kj_mol'] = None
        entry['criterion_met'] = False
        entry['candidate_angle_deg'] = None
    evaluation.append(entry)

refine = []
for cand in recommended:
    if len(refine) >= 2:
        break
    cand = round(cand / 5.0) * 5.0
    if abs(cand) >= 179.999:
        continue
    if all(abs(cand - c) >= 15.0 for c in refine):
        refine.append(cand)

all_sources = []
for m in members:
    rec = records.get(m['request_key'])
    if rec is None:
        continue
    all_sources.append(src(rec, 'stdout.out', 'FINAL SINGLE POINT ENERGY and optimization convergence flags'))
    all_sources.append(src(rec, 'job.xyz', 'final optimized geometry used to recompute the dihedral'))
local_source = {'filename': 'scan_results.json', 'location': 'members[] and failed[] written incrementally'}
base_limitations = [
    'Constrained optimizations at fixed central C-C-C-C dihedral; electronic energies only, no ZPE/thermal/entropy corrections.',
    'A discrete grid maximum is a scan point, not a verified transition state; no frequency analysis or reaction path validation was done.',
]
artifacts_xyz = sorted(m['final_geometry_file'] for m in members if m.get('final_geometry_file'))
input_files = [m['input_file'] for m in members]

observations = [
    {'name': 'sampling',
     'value': {'requested_target_angles_deg': ANGLES,
               'computed_target_angles_deg': [m['target_angle_deg'] for m in sorted(members, key=lambda x: x['target_angle_deg'])],
               'not_completed_target_angles_deg': [f['target_angle_deg'] for f in failed],
               'constrained_atom_indices_0based': CONS,
               'constrained_atom_elements': [initial_atoms[i][0] for i in CONS],
               'grid_spacing_deg': 60.0,
               'job_type': 'constrained geometry optimization (Opt with %geom Constraints dihedral)'},
     'unit': 'degree',
     'sources': all_sources + [local_source],
     'formula': None,
     'artifacts': input_files + ['scan_results.json'],
     'limitations': base_limitations},
    {'name': 'energy',
     'value': {'angles_deg': [m['target_angle_deg'] for m in ordered],
               'energies_hartree': [m['energy_hartree'] for m in ordered],
               'electronic_energies_only': True},
     'unit': 'hartree',
     'sources': all_sources + [local_source],
     'formula': None,
     'artifacts': ['scan_results.json'],
     'limitations': base_limitations},
    {'name': 'energy_difference',
     'value': rel,
     'unit': 'kilojoule per mole',
     'sources': all_sources + [local_source],
     'formula': 'dE(angle) = (E(angle) - min over computed angles of E) * 2625.4996394799 kJ/mol per hartree',
     'artifacts': ['scan_results.json'],
     'limitations': base_limitations + ['Reference is the lowest computed coarse-scan point, not a zero-point-corrected or global minimum.']},
    {'name': 'member_table',
     'value': {'members': sorted(members, key=lambda x: x['target_angle_deg']), 'failed_members': failed},
     'unit': 'hartree',
     'sources': all_sources + [local_source],
     'formula': None,
     'artifacts': input_files + ['scan_results.json'] + artifacts_xyz,
     'limitations': base_limitations + ['operation_status is receipt metadata, not independent scientific validation.']},
    {'name': 'optimized_geometry',
     'value': [{'target_angle_deg': m['target_angle_deg'],
                'actual_dihedral_deg': m['actual_dihedral_deg'],
                'atom_count': m['atom_count'],
                'xyz_file': m['final_geometry_file'],
                'optimization_converged': m['optimization_converged']} for m in sorted(members, key=lambda x: x['target_angle_deg'])],
     'unit': 'angstrom',
     'sources': all_sources + [local_source],
     'formula': 'dihedral i-j-k-l recomputed from the final optimized Cartesian coordinates using the atan2(dot(m1,n2), dot(n1,n2)) formulation with n1 = b0 x b1, n2 = b1 x b2, m1 = n1 x unit(b1)',
     'artifacts': artifacts_xyz + ['scan_results.json'],
     'limitations': ['Only members with an available final geometry have a saved XYZ; missing/unconverged members are listed in member_table.failed_members.']},
    {'name': 'sampling_check',
     'value': {'criterion': CRITERION, 'evaluation': evaluation,
               'pre_screen_candidate_angles_deg': refine,
               'decision_status': 'deferred_to_main_decision_loop'},
     'unit': None,
     'sources': all_sources + [local_source],
     'formula': 'three-point parabola through (x-h,y1),(x,y2),(x+h,y3): b=(y3-y1)/2, a=(y1+y3)/2-y2, vertex offset = -b/(2a)*h, predicted lowering = b^2/(4a)*2625.4996394799',
     'artifacts': ['scan_results.json'],
     'limitations': ['The criterion was applied only to complete triplets; the continue/stop decision and any refinement jobs are taken later in the main decision loop, not here.']},
    {'name': 'random_seed',
     'value': {'declared_seed': 1701,
               'random_numbers_used_in_scan': False,
               'initial_geometry_generation': {'generator': 'opi.Structure.from_smiles', 'smiles': 'CCCC', 'random_seed_exposed': None},
               'note': 'Dihedral scan, input construction, parsing and analysis are deterministic and invoke no random number generator. The registered initial conformer was generated from SMILES during structure preparation and exposes no seed; the archived geometry is reused unchanged.'},
     'unit': None,
     'sources': [local_source],
     'formula': None,
     'artifacts': ['scan_results.json'],
     'limitations': ['Seed 1701 cannot be retroactively imposed on the registered initial conformer; this is recorded rather than fabricated as reproducible.']},
]

Path('observations.json').write_text(json.dumps({'version': 1, 'observations': observations}, indent=2), encoding='utf-8')
print('members:', [(m['target_angle_deg'], m['actual_dihedral_deg'], m['energy_hartree'], m['optimization_converged']) for m in sorted(members, key=lambda x: x['target_angle_deg'])])
print('relative energies:', rel)
print('failed:', failed)
print('pre-screen refinement candidates:', refine)
