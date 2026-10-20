import json, math, os
from rdkit import Chem
from rdkit.Chem import AllChem, rdMolAlign
from orca_task_sdk import run_orca, receipt_source

SEED = 1701
THRESH = 0.15
N_EMBED = 60
N_ORCA = 5
HKJ = 2625.499639
METHOD = 'B3LYP'
BASIS = 'def2-SVP'
CHARGE = 0
MULT = 1


def conf_xyz(mol, cid, comment):
    c = mol.GetConformer(cid)
    out = [str(mol.GetNumAtoms()), comment]
    for i, a in enumerate(mol.GetAtoms()):
        p = c.GetAtomPosition(i)
        out.append('%s %.6f %.6f %.6f' % (a.GetSymbol(), p.x, p.y, p.z))
    return '\n'.join(out) + '\n'


mol = Chem.AddHs(Chem.MolFromSmiles('CCCCCC'))
ps = AllChem.ETKDGv3()
ps.randomSeed = SEED
ps.numThreads = 1
ps.pruneRmsThresh = -1.0
AllChem.EmbedMultipleConfs(mol, numConfs=N_EMBED, params=ps)
conf_ids = [c.GetId() for c in mol.GetConformers()]
ffres = AllChem.MMFFOptimizeMoleculeConfs(mol, maxIters=2000, numThreads=1)

cands = []
for i, cid in enumerate(conf_ids):
    nconv, e = ffres[i]
    fn = 'cand_%02d.xyz' % (i + 1)
    with open(fn, 'w') as fh:
        fh.write(conf_xyz(mol, cid, 'n-hexane %s RDKit ETKDGv3 seed=1701 MMFF94' % fn))
    cands.append({'id': fn[:-4], 'file': fn, 'cid': cid,
                  'mmff_kcal_mol': float(e), 'ff_converged': bool(nconv == 0)})


def heavy_conf(cid):
    m = Chem.Mol(mol)
    m.RemoveAllConformers()
    m.AddConformer(Chem.Conformer(mol.GetConformer(cid)), assignId=True)
    return Chem.RemoveHs(m)


def heavy_rmsd(h1, h2):
    try:
        return float(rdMolAlign.GetBestRMS(h1, h2, prbId=0, refId=0))
    except Exception:
        c1 = h1.GetConformer(0)
        c2 = h2.GetConformer(0)
        n = h1.GetNumAtoms()
        best = None
        for rev in (False, True):
            s = 0.0
            for i in range(n):
                p = c1.GetAtomPosition(i)
                q = c2.GetAtomPosition(n - 1 - i if rev else i)
                s += (p.x - q.x) ** 2 + (p.y - q.y) ** 2 + (p.z - q.z) ** 2
            r = math.sqrt(s / n)
            best = r if best is None else min(best, r)
        return float(best)


byid = {}
heavies = {}
kept = []
for cand in sorted(cands, key=lambda r: (r['mmff_kcal_mol'], r['id'])):
    h = heavy_conf(cand['cid'])
    heavies[cand['id']] = h
    byid[cand['id']] = cand
    best = None
    bestid = None
    for k in kept:
        r = heavy_rmsd(h, heavies[k])
        if best is None or r < best:
            best = r
            bestid = k
    cand['min_rmsd_A'] = None if best is None else round(best, 4)
    if best is not None and best < THRESH:
        cand['duplicate_of'] = bestid
    else:
        cand['duplicate_of'] = None
        kept.append(cand['id'])

selected_ids = kept[:N_ORCA]

ORCA_INP = '! RKS B3LYP def2-SVP SP NoAutoStart\n%maxcore 192\n%scf\n  MaxIter 256\n  AutoTRAH false\nend\n* xyzfile 0 1 {xyz}\n'

runs = []
for cid_ in selected_ids:
    c = byid[cid_]
    inp = 'sp_%s.inp' % cid_
    with open(inp, 'w') as fh:
        fh.write(ORCA_INP.format(xyz=c['file']))
    rec = run_orca(inp, dependencies=[c['file']], request_key='sp_' + cid_, purpose='new_candidate')
    energy = None
    files = rec.get('files') or {}
    p = files.get('stdout.out')
    if p and os.path.exists(p):
        try:
            txt = open(p, 'r', errors='replace').read()
        except Exception:
            txt = ''
        for line in txt.splitlines():
            s = line.strip()
            if s.startswith('FINAL SINGLE POINT ENERGY'):
                try:
                    energy = float(s.split()[-1])
                except Exception:
                    pass
    try:
        src = receipt_source(rec, 'stdout.out', location='FINAL SINGLE POINT ENERGY (last occurrence)')
    except Exception:
        src = {'internal_request_key': rec.get('request_key'), 'filename': 'stdout.out',
               'location': 'FINAL SINGLE POINT ENERGY (last occurrence)'}
    runs.append({'candidate': cid_, 'xyz': c['file'], 'input': inp,
                 'energy_hartree': energy, 'source': src})

ok = [r for r in runs if r['energy_hartree'] is not None]
low = None
if ok:
    emin = min(r['energy_hartree'] for r in ok)
    for r in ok:
        r['relative_kj_mol'] = round((r['energy_hartree'] - emin) * HKJ, 4)
    low = min(ok, key=lambda r: r['energy_hartree'])['candidate']
for r in runs:
    if r['energy_hartree'] is None:
        r['relative_kj_mol'] = None

all_xyz = [c['file'] for c in cands]
all_inp = [r['input'] for r in runs]
nconv_true = sum(1 for c in cands if c['ff_converged'])
dups = [{'candidate': c['id'], 'duplicate_of': c['duplicate_of'],
         'min_rmsd_A': c['min_rmsd_A'], 'mmff_energy_kcal_mol': c['mmff_kcal_mol']}
        for c in cands if c['duplicate_of']]

obs = []
obs.append({
    'name': 'candidate_conformer_search',
    'value': {
        'system': 'n-hexane',
        'smiles': 'CCCCCC',
        'generator': 'RDKit ETKDGv3',
        'random_seed': SEED,
        'num_conformers_requested': N_EMBED,
        'num_conformers_embedded': len(cands),
        'force_field': 'MMFF94',
        'num_ff_converged': nconv_true,
        'duplicate_rmsd_threshold_A': THRESH,
        'rmsd_definition': 'heavy_atom_symmetry_aware_best_rmsd_covering_reversed_chain_equivalence',
        'candidates': [{'candidate': c['id'], 'xyz_file': c['file'],
                        'mmff_energy_kcal_mol': c['mmff_kcal_mol'],
                        'ff_converged': c['ff_converged'],
                        'duplicate_of': c['duplicate_of'],
                        'min_rmsd_to_earlier_kept_A': c['min_rmsd_A']} for c in cands],
        'num_unique_clusters': len(kept)
    },
    'unit': None,
    'sources': [{'filename': f, 'location': None} for f in all_xyz],
    'formula': None,
    'artifacts': list(all_xyz),
    'limitations': [
        'ETKDGv3 embedding is stochastic but was run with fixed randomSeed=1701; MMFF94 minimization is deterministic given coordinates.',
        'Sampling is finite (60 embedded conformers from one SMILES); no systematic torsion scan or solvation was performed.',
        'Force-field convergence indicates MMFF94 minimization convergence only, not global optimality.'
    ]
})
obs.append({
    'name': 'orca_single_point_energy_comparison',
    'value': {
        'program': 'ORCA 6.1',
        'method': METHOD,
        'basis': BASIS,
        'charge': CHARGE,
        'multiplicity': MULT,
        'electronic_state': 'RKS',
        'environment': 'gas_phase',
        'scf_settings': 'MaxIter 256; AutoTRAH false',
        'maxcore_mb_per_process': 192,
        'job_type': 'single_point',
        'energies': [{'candidate': r['candidate'], 'xyz_file': r['xyz'], 'input_file': r['input'],
                      'total_energy_hartree': r['energy_hartree'],
                      'relative_to_lowest_kj_mol': r['relative_kj_mol']} for r in runs],
        'lowest_candidate': low
    },
    'unit': None,
    'sources': [r['source'] for r in runs],
    'formula': 'relative_kj_mol = (E_total_hartree - E_lowest_hartree) * 2625.499639',
    'artifacts': list(all_xyz) + list(all_inp),
    'limitations': [
        'Single-point electronic energies only; no zero-point, thermal or entropy correction, so the ranking is not a free-energy ranking.',
        'Only the finite subset selected for ORCA was computed at B3LYP/def2-SVP; energies are not comparable to other levels of theory.',
        'B3LYP/def2-SVP was chosen as a widely used hybrid functional with a polarized double-zeta basis suitable for organic conformer comparisons within the run budget.'
    ]
})
obs.append({
    'name': 'candidate_delivery_and_energy_ranking',
    'value': {
        'total_candidates_produced': len(cands),
        'produced_candidates': [{'candidate': c['id'], 'xyz_file': c['file']} for c in cands],
        'selected_for_orca': list(selected_ids),
        'excluded_as_duplicates': dups,
        'excluded_from_selection_reason': 'not among the 5 lowest-MMFF98 distinct clusters selected for ORCA single points',
        'ranking': sorted([{'candidate': r['candidate'],
                            'total_energy_hartree': r['energy_hartree'],
                            'relative_to_lowest_kj_mol': r['relative_kj_mol']}
                           for r in ok], key=lambda x: x['total_energy_hartree']),
        'lowest_candidate_structure': low,
        'lowest_candidate_xyz_file': (byid[low]['file'] if low else None),
        'unresolved_orca_runs': [r['candidate'] for r in runs if r['energy_hartree'] is None]
    },
    'unit': None,
    'sources': [{'filename': f, 'location': None} for f in all_xyz] + [r['source'] for r in runs],
    'formula': None,
    'artifacts': list(all_xyz) + list(all_inp),
    'limitations': [
        'Every actually generated candidate (all embedded/optimized conformers) is listed, including duplicates that were excluded from the ORCA comparison.',
        'Candidates without a parsed ORCA FINAL SINGLE POINT ENERGY are never reported as having ORCA energies.'
    ]
})
obs.append({
    'name': 'random_seed_record',
    'value': {
        'seed': SEED,
        'applied_to': 'RDKit ETKDGv3 randomSeed',
        'seed_supported_by_library': True,
        'deterministic_steps': ['MMFF94 minimization'],
        'note': 'All stochastic structure generation used seed 1701; no unfixable random step exists in this workflow.'
    },
    'unit': None,
    'sources': [{'filename': f, 'location': None} for f in all_xyz],
    'formula': None,
    'artifacts': list(all_xyz),
    'limitations': ['Bitwise reproducibility across different RDKit versions/platforms is not guaranteed even with a fixed seed.']
})
obs.append({
    'name': 'search_limitations_disclosure',
    'value': {
        'search_scope': '60 ETKDGv3 embeddings of C6H14 from SMILES CCCCCC, MMFF94 minimized (max 2000 iterations, 1 thread), vacuum, no torsion scan, no solvation.',
        'geometry_source': 'RDKit-generated 3D conformers (all-atom XYZ files saved per candidate).',
        'global_minimum_claim': False,
        'statement': 'The lowest ORCA energy found is the lowest within this finite candidate set only; it is not a global minimum and not a free-energy minimum.',
        'force_field_prefilter': {'force_field': 'MMFF94', 'num_candidates': len(cands), 'num_converged': nconv_true},
        'ff_convergence_meaning': 'MMFF94 optimization converged for the reported count; this does not imply a complete or exhaustive conformational search.'
    },
    'unit': None,
    'sources': [{'filename': f, 'location': None} for f in all_xyz],
    'formula': None,
    'artifacts': list(all_xyz),
    'limitations': ['Finite stochastic search; absence of other low-energy conformers is not proven.']
})

with open('observations.json', 'w') as fh:
    json.dump({'version': 1, 'observations': obs}, fh, indent=1)
print('candidates=%d unique=%d selected=%d orca_ok=%d lowest=%s' % (len(cands), len(kept), len(selected_ids), len(ok), low))
print(json.dumps([{'candidate': r['candidate'], 'E': r['energy_hartree'], 'rel_kJ': r['relative_kj_mol']} for r in ok], indent=1))
