import json, os, re, shutil, math
from pathlib import Path
import numpy as np
from rdkit import Chem
from rdkit.Chem import AllChem
from orca_task_sdk import run_orca

SEED = 1701
THRESH = 0.15
NUM_EMBED = 64
N_SELECT = 4

Path('candidates').mkdir(exist_ok=True)
Path('orca_results').mkdir(exist_ok=True)

def parse_xyz(path):
    lines = Path(path).read_text().splitlines()
    n = int(lines[0].strip())
    atoms = []
    for line in lines[2:2+n]:
        p = line.split()
        atoms.append((p[0], np.array([float(p[1]), float(p[2]), float(p[3])], float)))
    return atoms

def write_xyz(path, symbols, coords, comment=''):
    with open(path, 'w') as f:
        f.write(f'{len(symbols)}\n{comment}\n')
        for s, c in zip(symbols, coords):
            f.write(f'{s} {c[0]:.10f} {c[1]:.10f} {c[2]:.10f}\n')

def kabsch_rmsd(P, Q):
    P = np.asarray(P, float); Q = np.asarray(Q, float)
    Pc = P - P.mean(axis=0); Qc = Q - Q.mean(axis=0)
    H = Pc.T @ Qc
    U, S, Vt = np.linalg.svd(H)
    d = np.sign(np.linalg.det(Vt.T @ U.T))
    D = np.diag([1.0, 1.0, d])
    R = Vt.T @ D @ U.T
    Pr = Pc @ R.T
    return float(np.sqrt(np.mean(np.sum((Pr - Qc) ** 2, axis=1))))

def mol_to_ordered_xyz(mol, conf_id, seed_symbols):
    conf = mol.GetConformer(int(conf_id))
    atoms = []
    for i, a in enumerate(mol.GetAtoms()):
        p = conf.GetAtomPosition(i)
        atoms.append((a.GetSymbol(), np.array([p.x, p.y, p.z], float)))
    buckets = {}
    for idx, (sym, pos) in enumerate(atoms):
        buckets.setdefault(sym, []).append(idx)
    order = []
    for sym in seed_symbols:
        if not buckets.get(sym):
            raise RuntimeError('element count/order mismatch with seed')
        order.append(buckets[sym].pop(0))
    if len(order) != len(atoms):
        raise RuntimeError('atom count mismatch with seed')
    return [atoms[i][0] for i in order], np.array([atoms[i][1] for i in order], float)

seed_atoms = parse_xyz('seed.xyz')
seed_symbols = [s for s, c in seed_atoms]
shutil.copyfile('seed.xyz', 'initial_seed.xyz')

mol = Chem.AddHs(Chem.MolFromSmiles('CCCCCC'))
params = AllChem.ETKDGv3()
params.randomSeed = SEED
params.numThreads = 1
params.pruneRmsThresh = -1.0
cids = list(AllChem.EmbedMultipleConfs(mol, numConfs=NUM_EMBED, params=params))
if len(cids) < 8:
    raise RuntimeError(f'only {len(cids)} conformers embedded; need at least 8')

try:
    if AllChem.MMFFHasAllMoleculeParams(mol):
        ffname = 'MMFF94'
        ffres = AllChem.MMFFOptimizeMoleculeConfs(mol, maxIters=2000, numThreads=1)
    else:
        ffname = 'UFF'
        ffres = AllChem.UFFOptimizeMoleculeConfs(mol, maxIters=2000, numThreads=1)
except Exception:
    ffname = 'UFF_fallback'
    ffres = AllChem.UFFOptimizeMoleculeConfs(mol, maxIters=2000, numThreads=1)

candidates = []
for j, cid in enumerate(cids, start=1):
    syms, coords = mol_to_ordered_xyz(mol, cid, seed_symbols)
    fn = f'candidates/cand_{j:02d}.xyz'
    write_xyz(fn, syms, coords, comment=f'candidate cand_{j:02d} RDKit ETKDGv3 seed={SEED} {ffname}')
    conv = bool(ffres[j-1][0] == 0)
    energy = float(ffres[j-1][1])
    candidates.append({
        'id': f'cand_{j:02d}',
        'file': fn,
        'rdkit_conf_id': int(cid),
        'forcefield': ffname,
        'forcefield_converged': conv,
        'forcefield_energy_kcal_mol': energy,
        'symbols': syms,
        'coords': coords.tolist(),
        'carbon_coords': [c.tolist() for s, c in zip(syms, coords) if s == 'C'],
    })

pairwise = {}
for i in range(len(candidates)):
    for j in range(i+1, len(candidates)):
        a, b = candidates[i], candidates[j]
        P, Q = np.array(a['carbon_coords']), np.array(b['carbon_coords'])
        r_id = kabsch_rmsd(P, Q)
        r_rev = kabsch_rmsd(P, Q[::-1])
        pairwise[f'{a["id"]}|{b["id"]}'] = {
            'rmsd_identity_angstrom': r_id,
            'rmsd_reversed_angstrom': r_rev,
            'rmsd_min_angstrom': min(r_id, r_rev),
            'duplicate_at_0.15_angstrom': bool(min(r_id, r_rev) < THRESH),
        }

selected = []
for cand in sorted(candidates, key=lambda c: (not c['forcefield_converged'], c['forcefield_energy_kcal_mol'], c['id'])):
    if len(selected) >= N_SELECT:
        break
    if not cand['forcefield_converged']:
        cand['exclusion_reason'] = 'forcefield_not_converged'
        continue
    dup = None
    for s in selected:
        key = f'{cand["id"]}|{s["id"]}' if f'{cand["id"]}|{s["id"]}' in pairwise else f'{s["id"]}|{cand["id"]}'
        val = pairwise[key]['rmsd_min_angstrom']
        if val < THRESH:
            dup = (s['id'], val)
            break
    if dup:
        cand['exclusion_reason'] = f'duplicate_of_{dup[0]}_rmsd_{dup[1]:.4f}_angstrom'
    else:
        selected.append(cand)
        cand['status'] = 'selected'

selected_ids = {c['id'] for c in selected}
for cand in candidates:
    if cand['id'] in selected_ids:
        cand['status'] = 'selected'
        cand['exclusion_reason'] = None
    else:
        cand.setdefault('status', 'excluded')
        cand.setdefault('exclusion_reason', 'not_selected_limited_to_4_distinct_conformers')

manifest = {
    'version': 1,
    'search_seed': SEED,
    'duplicate_rmsd_threshold_angstrom': THRESH,
    'forcefield': ffname,
    'n_embedded': len(cids),
    'candidates': candidates,
    'selected_ids': [c['id'] for c in selected],
    'pairwise_carbon_chain_rmsd': pairwise,
    'seed_file': 'initial_seed.xyz',
    'notes': ['All embedded conformers saved; selected subset is not a global minimum claim.'],
}
Path('candidates_manifest.json').write_text(json.dumps(manifest, indent=2))

provenance = {
    'version': 1,
    'program': 'conformer_search.py',
    'search_scope': f'RDKit ETKDGv3 embedded {len(cids)} conformers from SMILES CCCCCC, all saved as candidates.',
    'geometry_source': 'Initial geometry from managed structure.prepare seed.xyz; task candidates generated from RDKit and ordered to match seed element sequence.',
    'random_seed': SEED,
    'forcefield': ffname,
    'forcefield_converged_count': sum(1 for c in candidates if c['forcefield_converged']),
    'forcefield_not_converged_count': sum(1 for c in candidates if not c['forcefield_converged']),
    'duplicate_check': 'Kabsch-aligned RMSD over six carbon atoms using min(identity, reversed carbon-chain order), threshold 0.15 A.',
    'method_rationale': 'r2SCAN-3c is a self-contained composite method with dispersion suitable for relative single-point energies of flexible alkanes; gas phase, neutral, singlet, RKS.',
    'limitations': ['Finite conformer search, not global or free-energy minimum.', 'Force-field pre-screen is not an ORCA energy.'],
}
Path('search_provenance.json').write_text(json.dumps(provenance, indent=2))

orca_results = []
for idx, cand in enumerate(selected, start=1):
    xyz_name = f'sp_candidate_{idx:02d}.xyz'
    shutil.copyfile(cand['file'], xyz_name)
    inp_name = f'sp_candidate_{idx:02d}.inp'
    inp = (
        '! r2SCAN-3c RKS NoAutoStart\n'
        '%maxcore 192\n'
        '%scf\n'
        '  MaxIter 256\n'
        '  AutoTRAH false\n'
        'end\n'
        f'* xyzfile 0 1 {xyz_name}\n'
    )
    Path(inp_name).write_text(inp)
    receipt = run_orca(inp_name, dependencies=(xyz_name,), request_key=f'sp_{cand["id"]}', purpose='new_candidate')
    files = receipt.get('files', {})
    stdout_key = None
    for k in files:
        if k == 'stdout.out' or k.endswith('/stdout.out') or k.endswith('stdout.out'):
            stdout_key = k
            break
    text = ''
    if stdout_key:
        text = Path(files[stdout_key]).read_text(errors='replace')
    m = re.findall(r'FINAL SINGLE POINT ENERGY\s+([-+]?\d+(?:\.\d+)?)', text)
    e_h = float(m[-1]) if m else None
    rec = {
        'candidate_id': cand['id'],
        'candidate_xyz': cand['file'],
        'orca_input': inp_name,
        'orca_dependency': xyz_name,
        'request_key': receipt.get('request_key', f'sp_{cand["id"]}'),
        'operation_status': receipt.get('operation_status'),
        'scientific_validation': receipt.get('scientific_validation'),
        'actual_conditions': receipt.get('actual_conditions'),
        'files_keys': list(files.keys()),
        'stdout_key': stdout_key,
        'final_single_point_energy_hartree': e_h,
        'conditions_declared': {'method': 'r2SCAN-3c', 'basis': 'built-in', 'charge': 0, 'multiplicity': 1, 'electronic_state': 'RKS', 'environment': 'gas_phase', 'maxcore_mb': 192, 'scf_maxiter': 256, 'autotrah': False},
    }
    orca_results.append(rec)
    Path(f'orca_results/{cand["id"]}.json').write_text(json.dumps(rec, indent=2))

valid = [r for r in orca_results if r['final_single_point_energy_hartree'] is not None]
min_e = min((r['final_single_point_energy_hartree'] for r in valid), default=None)
for r in orca_results:
    e = r['final_single_point_energy_hartree']
    r['relative_kj_mol'] = None if (e is None or min_e is None) else (e - min_e) * 2625.499638
ranking = sorted(valid, key=lambda r: r['final_single_point_energy_hartree'])
for rank, r in enumerate(ranking, start=1):
    r['rank'] = rank

obs = []
all_candidate_files = [c['file'] for c in candidates]
obs.append({
    'name': 'candidate_structures',
    'value': {'count': len(candidates), 'files': all_candidate_files, 'manifest': 'candidates_manifest.json', 'selected_ids': [c['id'] for c in selected], 'seed': SEED, 'duplicate_threshold_angstrom': THRESH},
    'unit': None,
    'sources': [{'filename': 'candidates_manifest.json', 'location': 'candidates[]'}],
    'formula': None,
    'artifacts': all_candidate_files + ['candidates_manifest.json', 'initial_seed.xyz'],
    'limitations': ['Finite conformer search; not a global minimum claim.'],
})
obs.append({
    'name': 'search_filter_program',
    'value': {'program': 'conformer_search.py', 'algorithm': 'RDKit ETKDGv3 fixed seed 1701; force-field optimization; carbon-chain Kabsch RMSD duplicate check with reversed-order equivalence; 0.15 A threshold; greedy distinct selection; ORCA r2SCAN-3c single points.', 'search_seed': SEED, 'duplicate_threshold_angstrom': THRESH},
    'unit': None,
    'sources': [{'filename': 'search_provenance.json', 'location': 'program/algorithm'}],
    'formula': None,
    'artifacts': ['search_provenance.json', 'candidates_manifest.json'],
    'limitations': ['Program is the executed Python code artifact; provenance JSON summarizes it.'],
})
obs.append({
    'name': 'selected_conformers',
    'value': {'selected_ids': [c['id'] for c in selected], 'files': [c['file'] for c in selected], 'selection_rule': 'force-field energy ascending, pairwise carbon-chain RMSD >= 0.15 A under identity/reversed mapping'},
    'unit': None,
    'sources': [{'filename': 'candidates_manifest.json', 'location': 'selected_ids'}],
    'formula': None,
    'artifacts': [c['file'] for c in selected] + ['candidates_manifest.json'],
    'limitations': ['Selected subset is from a finite search, not global minimization.'],
})
energy_sources = []
for r in orca_results:
    if r.get('stdout_key'):
        energy_sources.append({'internal_request_key': r['request_key'], 'filename': r['stdout_key'], 'location': 'FINAL SINGLE POINT ENERGY'})
obs.append({
    'name': 'energy',
    'value': {r['candidate_id']: {'absolute_energy_hartree': r['final_single_point_energy_hartree'], 'relative_kj_mol': r.get('relative_kj_mol')} for r in orca_results},
    'unit': 'hartree (absolute); kJ/mol (relative)',
    'sources': energy_sources + [{'filename': 'orca_results', 'location': 'per-candidate JSON'}],
    'formula': 'relative_kJ_mol = (E_candidate - E_lowest_in_this_finite_set) * 2625.499638',
    'artifacts': [f'orca_results/{r["candidate_id"]}.json' for r in orca_results],
    'limitations': ['Only selected candidates were computed by ORCA; force-field energies are not ORCA energies.', 'Finite search; lowest is not global/free-energy minimum.'],
})
obs.append({
    'name': 'conformer_duplicate_check',
    'value': {'threshold_angstrom': THRESH, 'method': 'Kabsch RMSD on six carbon atoms; min(identity, reversed carbon-chain order)', 'duplicate_pairs': {k: v for k, v in pairwise.items() if v['duplicate_at_0.15_angstrom']}, 'n_duplicate_pairs': sum(1 for v in pairwise.values() if v['duplicate_at_0.15_angstrom'])},
    'unit': 'angstrom',
    'sources': [{'filename': 'candidates_manifest.json', 'location': 'pairwise_carbon_chain_rmsd'}],
    'formula': 'RMSD = sqrt(mean_i ||R(P_i - mean(P)) - (Q_i - mean(Q))||^2), evaluated for Q and Q_reversed',
    'artifacts': ['candidates_manifest.json'],
    'limitations': ['Heavy-atom RMSD uses carbon atoms because n-hexane has only carbon heavy atoms.'],
})
obs.append({
    'name': 'energy_ranking',
    'value': {'absolute_energies_hartree': {r['candidate_id']: r['final_single_point_energy_hartree'] for r in orca_results}, 'relative_kj_mol': {r['candidate_id']: r.get('relative_kj_mol') for r in orca_results}, 'ranking': [{'rank': r.get('rank'), 'candidate_id': r['candidate_id'], 'absolute_energy_hartree': r['final_single_point_energy_hartree'], 'relative_kj_mol': r.get('relative_kj_mol')} for r in ranking], 'minimum_candidate': None if not ranking else ranking[0]['candidate_id'], 'minimum_structure': None if not ranking else ranking[0]['candidate_xyz']},
    'unit': 'hartree and kJ/mol',
    'sources': energy_sources,
    'formula': 'relative_kJ_mol = (E - E_min_in_this_finite_set) * 2625.499638',
    'artifacts': [f'orca_results/{r["candidate_id"]}.json' for r in orca_results],
    'limitations': ['Lowest in this finite search only; not global or free-energy minimum.'],
})
obs.append({
    'name': 'search_provenance_report',
    'value': {'search_scope': provenance['search_scope'], 'geometry_source': provenance['geometry_source'], 'forcefield': ffname, 'forcefield_converged_count': provenance['forcefield_converged_count'], 'forcefield_not_converged_count': provenance['forcefield_not_converged_count'], 'method': 'r2SCAN-3c', 'basis': 'built-in', 'method_rationale': provenance['method_rationale'], 'limitations': provenance['limitations']},
    'unit': None,
    'sources': [{'filename': 'search_provenance.json', 'location': 'search_scope/geometry_source/forcefield'}, {'filename': 'candidates_manifest.json', 'location': 'candidates[].forcefield_converged'}],
    'formula': None,
    'artifacts': ['search_provenance.json', 'candidates_manifest.json', 'initial_seed.xyz'],
    'limitations': ['Force-field convergence is reported per generated candidate; not every candidate was ORCA-evaluated.'],
})
obs.append({
    'name': 'random_seed',
    'value': {'seed': SEED, 'fixed_for': ['RDKit ETKDGv3 randomSeed'], 'not_fixable': [], 'note': 'RDKit ETKDGv3 accepts a fixed randomSeed; the seed is recorded in manifests and observations.'},
    'unit': None,
    'sources': [{'filename': 'candidates_manifest.json', 'location': 'search_seed'}, {'filename': 'search_provenance.json', 'location': 'random_seed'}],
    'formula': None,
    'artifacts': ['candidates_manifest.json', 'search_provenance.json'],
    'limitations': ['No non-fixable random component was identified in the executed search.'],
})

Path('observations.json').write_text(json.dumps({'version': 1, 'observations': obs}, indent=2))
print('Wrote observations.json with', len(obs), 'observations and', len(orca_results), 'ORCA results')
