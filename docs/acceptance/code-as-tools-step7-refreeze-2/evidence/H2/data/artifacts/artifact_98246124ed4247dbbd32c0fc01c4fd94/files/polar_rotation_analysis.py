import json, math
import numpy as np
from orca_task_sdk import run_orca, receipt_source

NL = chr(10)
AXIS = np.array([1.0, 2.0, 3.0])
AXIS = AXIS / np.linalg.norm(AXIS)
ANGLE_DEG = 37.0
THETA = math.radians(ANGLE_DEG)
K = np.array([[0.0, -AXIS[2], AXIS[1]], [AXIS[2], 0.0, -AXIS[0]], [-AXIS[1], AXIS[0], 0.0]])
R = math.cos(THETA) * np.eye(3) + math.sin(THETA) * K + (1.0 - math.cos(THETA)) * np.outer(AXIS, AXIS)

def read_xyz(path):
    lines = open(path, 'r').read().splitlines()
    n = int(lines[0].strip())
    symbols = []
    coords = []
    for ln in lines[2:2+n]:
        p = ln.split()
        symbols.append(p[0])
        coords.append([float(p[1]), float(p[2]), float(p[3])])
    return symbols, np.array(coords)

symbols, coords = read_xyz('prepared.xyz')
rot = (R @ coords.T).T
with open('rotated.xyz', 'w') as f:
    f.write(str(len(symbols)) + NL)
    f.write('CH2O active rotation about origin; axis (1,2,3) normalized; right-hand rule; 37 deg' + NL)
    for s, c in zip(symbols, rot):
        f.write('%s %.12f %.12f %.12f%s' % (s, c[0], c[1], c[2], NL))

def pair_distances(c):
    return np.array([np.linalg.norm(c[i] - c[j]) for i in range(len(c)) for j in range(i + 1, len(c))])

d_before = pair_distances(coords)
d_after = pair_distances(rot)
max_dist_change = float(np.max(np.abs(d_after - d_before)))

rotation_record = {
    'center': 'origin of the input coordinate frame (0,0,0)',
    'axis_direction_in_original_frame': [1.0, 2.0, 3.0],
    'axis_unit_vector': [float(x) for x in AXIS],
    'angle_deg': ANGLE_DEG,
    'handedness': 'right-hand rule; active rotation of the frozen geometry',
    'rotation_matrix_row_major': [[float(x) for x in row] for row in R],
    'atom_order_preserved': symbols,
    'prepared_coordinates_angstrom': [[float(x) for x in row] for row in coords],
    'rotated_coordinates_angstrom': [[float(x) for x in row] for row in rot],
    'max_pair_distance_change_angstrom': max_dist_change,
    'formula': 'R = cos(t) I + sin(t) K + (1-cos(t)) a a^T, t=37 deg, a=(1,2,3)/sqrt(14)'
}
with open('rotation_record.json', 'w') as f:
    json.dump(rotation_record, f, indent=2)

seed_record = {
    'requested_seed': 1701,
    'random_numbers_used_in_authored_scripts': False,
    'rotation': 'analytic Rodrigues rotation; no random numbers',
    'orca': 'deterministic fixed-geometry electronic-structure input',
    'initial_structure_generation': {
        'generator': 'opi.Structure.from_smiles',
        'random_seed_reported': None,
        'seed_supported': False,
        'note': 'The prepared geometry came from the completed structure preparation step with no exposed controllable seed; reproducibility is provided by reusing the immutable prepared.xyz rather than regenerating it.'
    }
}
with open('random_seed_1701.json', 'w') as f:
    json.dump(seed_record, f, indent=2)

def checked_run(inp, deps, key):
    try:
        rec = run_orca(inp, dependencies=deps, request_key=key, purpose='new_candidate')
    except Exception as exc:
        with open('failed_' + key + '.json', 'w') as f:
            json.dump({'request_key': key, 'error': repr(exc)}, f)
        raise
    if rec.get('operation_status') != 'completed':
        with open('failed_' + key + '.json', 'w') as f:
            json.dump({'request_key': key, 'receipt': rec}, f)
        raise RuntimeError('ORCA run not completed for ' + key + ': ' + repr(rec.get('operation_status')))
    return rec

rec1 = checked_run('ch2o_pol_original.inp', ['prepared.xyz'], 'pol_original')
rec2 = checked_run('ch2o_pol_rotated.inp', ['rotated.xyz'], 'pol_rotated')

def parse_polar(rec):
    files = rec['files']
    if 'stdout.out' not in files:
        raise RuntimeError('stdout.out missing; keys=' + repr(list(files.keys())))
    text = open(files['stdout.out'], 'r', errors='replace').read()
    marker = 'raw cartesian tensor (atomic units):'
    i = text.find(marker)
    if i < 0:
        raise RuntimeError('raw cartesian tensor marker not found in stdout.out')
    j = text.find('diagonalized tensor:', i)
    if j < 0:
        raise RuntimeError('diagonalized tensor marker not found in stdout.out')
    block = text[i + len(marker):j]
    vals = []
    for tok in block.split():
        try:
            vals.append(float(tok))
        except ValueError:
            pass
    if len(vals) < 9:
        raise RuntimeError('parsed fewer than 9 tensor components: ' + repr(vals))
    tensor = np.array(vals[:9]).reshape(3, 3)
    k = text.find('Isotropic polarizability')
    if k < 0:
        raise RuntimeError('isotropic polarizability marker not found')
    tail = text[k:].split(':', 1)[1]
    iso = None
    for tok in tail.split():
        try:
            iso = float(tok)
            break
        except ValueError:
            continue
    if iso is None:
        raise RuntimeError('isotropic polarizability value not found')
    return tensor, iso

T1, iso1 = parse_polar(rec1)
T2, iso2 = parse_polar(rec2)
Tpred = R @ T1 @ R.T
tensor_max_abs_diff = float(np.max(np.abs(Tpred - T2)))
iso_from_trace_1 = float(np.trace(T1) / 3.0)
iso_from_trace_2 = float(np.trace(T2) / 3.0)
iso_reported_diff = float(iso2 - iso1)

with open('polarizability_table.csv', 'w') as f:
    f.write('quantity,row,col,value_atomic_units' + NL)
    for label, T in [('original', T1), ('rotated_reported', T2), ('original_RARt_prediction', Tpred)]:
        for i in range(3):
            for j in range(3):
                f.write('%s,%d,%d,%.10f%s' % (label, i + 1, j + 1, T[i, j], NL))
    f.write('original,isotropic,,%.10f%s' % (iso1, NL))
    f.write('rotated_reported,isotropic,,%.10f%s' % (iso2, NL))
    f.write('original,isotropic_from_trace,,%.10f%s' % (iso_from_trace_1, NL))
    f.write('rotated_reported,isotropic_from_trace,,%.10f%s' % (iso_from_trace_2, NL))

table_json = {
    'unit': 'atomic units',
    'original_tensor': [[float(x) for x in row] for row in T1],
    'rotated_reported_tensor': [[float(x) for x in row] for row in T2],
    'original_isotropic': iso1,
    'rotated_reported_isotropic': iso2,
    'original_isotropic_from_trace': iso_from_trace_1,
    'rotated_isotropic_from_trace': iso_from_trace_2
}
with open('polarizability_table.json', 'w') as f:
    json.dump(table_json, f, indent=2)

src1 = receipt_source(rec1, 'stdout.out', location='STATIC POLARIZABILITY TENSOR (Dipole/Dipole) and Isotropic polarizability')
src2 = receipt_source(rec2, 'stdout.out', location='STATIC POLARIZABILITY TENSOR (Dipole/Dipole) and Isotropic polarizability')

obs = [
    {'name': 'rotation_convention', 'value': rotation_record, 'unit': 'angstrom', 'sources': [{'filename': 'prepared.xyz'}, {'filename': 'rotated.xyz'}, {'filename': 'rotation_record.json'}], 'formula': rotation_record['formula'], 'artifacts': ['rotated.xyz', 'rotation_record.json'], 'limitations': ['Rotation applied to the frozen prepared geometry; no reoptimization.']},
    {'name': 'random_seed_record', 'value': seed_record, 'unit': None, 'sources': [{'filename': 'random_seed_1701.json'}], 'formula': None, 'artifacts': ['random_seed_1701.json'], 'limitations': ['Seed 1701 could not be imposed on opi.Structure.from_smiles because no seed control was exposed; determinism here comes from reusing the immutable prepared.xyz.']},
    {'name': 'pol_original_tensor', 'value': [[float(x) for x in row] for row in T1], 'unit': 'atomic units', 'sources': [src1], 'formula': 'RHF/def2-SVP fixed-geometry static dipole-dipole polarizability', 'artifacts': ['polarizability_table.csv', 'polarizability_table.json'], 'limitations': ['Raw cartesian tensor components as printed by ORCA.']},
    {'name': 'pol_original_isotropic', 'value': iso1, 'unit': 'atomic units', 'sources': [src1], 'formula': 'isotropic = Tr(alpha)/3', 'artifacts': ['polarizability_table.csv', 'polarizability_table.json'], 'limitations': ['Value as printed by ORCA.']},
    {'name': 'pol_rotated_tensor', 'value': [[float(x) for x in row] for row in T2], 'unit': 'atomic units', 'sources': [src2], 'formula': 'independent ORCA RHF/def2-SVP calculation on rotated.xyz', 'artifacts': ['polarizability_table.csv', 'polarizability_table.json'], 'limitations': ['Raw cartesian tensor components as printed by ORCA for the independently computed rotated geometry.']},
    {'name': 'pol_rotated_isotropic', 'value': iso2, 'unit': 'atomic units', 'sources': [src2], 'formula': 'isotropic = Tr(alpha)/3', 'artifacts': ['polarizability_table.csv', 'polarizability_table.json'], 'limitations': ['Value as printed by ORCA.']},
    {'name': 'tensor_transformation_check', 'value': {'rotation_matrix_row_major': [[float(x) for x in row] for row in R], 'max_abs_difference_between_reported_rotated_and_R_alpha_Rt': tensor_max_abs_diff, 'formula': 'alpha_rotated_predicted = R alpha_original R^T', 'reported_tr_original': float(np.trace(T1)), 'reported_tr_rotated': float(np.trace(T2))}, 'unit': 'atomic units', 'sources': [src1, src2], 'formula': 'alpha prime = R alpha R^T for an active rotation R of the frozen molecule', 'artifacts': ['polarizability_table.csv', 'polarizability_table.json'], 'limitations': ['Comparison is numerical and limited by ORCA print precision and the fixed-geometry approximation.']},
    {'name': 'isotropic_invariance_check', 'value': {'reported_isotropic_difference': iso_reported_diff, 'trace_derived_isotropic_original': iso_from_trace_1, 'trace_derived_isotropic_rotated': iso_from_trace_2, 'trace_derived_difference': float(iso_from_trace_2 - iso_from_trace_1)}, 'unit': 'atomic units', 'sources': [src1, src2], 'formula': 'alpha_iso = (alpha_xx + alpha_yy + alpha_zz)/3 is invariant under rotations', 'artifacts': ['polarizability_table.csv', 'polarizability_table.json'], 'limitations': ['Small differences reflect numerical precision of two independent SCF/CP-SCF calculations.']}
]

with open('observations.json', 'w') as f:
    json.dump({'version': 1, 'observations': obs}, f, indent=2)

print('rotation max pair-distance change (A):', max_dist_change)
print('original isotropic:', iso1)
print('rotated isotropic:', iso2)
print('max |R alpha R^T - rotated|:', tensor_max_abs_diff)
