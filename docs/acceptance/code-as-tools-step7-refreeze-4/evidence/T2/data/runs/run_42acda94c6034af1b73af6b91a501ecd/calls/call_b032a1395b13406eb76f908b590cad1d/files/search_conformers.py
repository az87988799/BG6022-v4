import json, math
from pathlib import Path
from rdkit import Chem
from rdkit.Chem import AllChem
import numpy as np
SEED=1701
N_CONF=48
N_SELECT=8
THRESH=0.15

def xyz_atoms(mol, cid):
    conf=mol.GetConformer(cid)
    atoms=[]
    for i,a in enumerate(mol.GetAtoms()):
        p=conf.GetAtomPosition(i)
        atoms.append({'element':a.GetSymbol(),'x':p.x,'y':p.y,'z':p.z})
    return atoms

def kabsch_rmsd(a,b):
    a=np.asarray(a,float)
    b=np.asarray(b,float)
    a=a-a.mean(0)
    b=b-b.mean(0)
    u,s,vt=np.linalg.svd(b.T@a)
    d=np.sign(np.linalg.det(u@vt))
    r=u@np.diag([1,1,d])@vt
    return float(np.sqrt(np.mean(np.sum((b@r-a)**2,axis=1))))

def heavy_rmsd(c1,c2):
    return min(kabsch_rmsd(c1,c2), kabsch_rmsd(c1,c2[::-1]))

prep=Path('prepared.xyz')
prep_text=prep.read_text() if prep.exists() else ''
anchor_elements=[]
if prep_text:
    lines=prep_text.splitlines()
    if len(lines)>2:
        for line in lines[2:]:
            parts=line.split()
            if len(parts)>=4:
                anchor_elements.append(parts[0])
mol=Chem.AddHs(Chem.MolFromSmiles('CCCCCC'))
params=AllChem.ETKDGv3()
params.randomSeed=SEED
params.useRandomCoords=True
cids=list(AllChem.EmbedMultipleConfs(mol, numConfs=N_CONF, params=params))
records=[]
for idx,cid in enumerate(cids):
    try:
        conv=AllChem.MMFFOptimizeMolecule(mol, confId=cid, maxIters=2000)
    except Exception:
        conv=1
    props=AllChem.MMFFGetMoleculeProperties(mol)
    ff=AllChem.MMFFGetMoleculeForceField(mol, props, confId=cid)
    if ff is None:
        raise RuntimeError('MMFF force field unavailable')
    e=float(ff.CalcEnergy())
    atoms=xyz_atoms(mol,cid)
    carbons=[(a['x'],a['y'],a['z']) for a in atoms if a['element']=='C']
    fname='candidate_%02d.xyz' % idx
    Path(fname).write_text(Chem.MolToXYZBlock(mol, confId=cid))
    records.append({'candidate_id':'c%02d' % idx,'filename':fname,'mmff_energy_kcal_mol':e,'force_field_converged':bool(conv==0),'atoms':atoms,'carbon_coords':carbons,'duplicate_of':None,'min_rmsd_to_retained':None,'selected':False,'exclusion_reason':None})
records.sort(key=lambda r: r['mmff_energy_kcal_mol'])
retained=[]
for r in records:
    best=None
    best_id=None
    for q in retained:
        d=heavy_rmsd(r['carbon_coords'], q['carbon_coords'])
        if best is None or d<best:
            best=d
            best_id=q['candidate_id']
    r['min_rmsd_to_retained']=best
    if best is not None and best < THRESH:
        r['duplicate_of']=best_id
        r['exclusion_reason']='duplicate: carbon-chain-reversal-aligned heavy-atom RMSD %.4f A < %.2f A to %s' % (best,THRESH,best_id)
    else:
        retained.append(r)
selected=retained[:N_SELECT]
selected_ids=set(r['candidate_id'] for r in selected)
for r in records:
    if r['candidate_id'] in selected_ids:
        r['selected']=True
        r['exclusion_reason']=None
    elif r['duplicate_of'] is None:
        r['exclusion_reason']='not selected: higher MMFF94 energy than the first 8 distinct retained conformers'
pair=[]
for i in range(len(selected)):
    for j in range(i+1,len(selected)):
        d=heavy_rmsd(selected[i]['carbon_coords'], selected[j]['carbon_coords'])
        pair.append({'a':selected[i]['candidate_id'],'b':selected[j]['candidate_id'],'rmsd_angstrom':d})
Path('members.json').write_text(json.dumps(records,indent=2))
Path('selected.json').write_text(json.dumps({'seed':SEED,'n_generated':len(records),'n_unique':len(retained),'n_selected':len(selected),'selected':[{'candidate_id':r['candidate_id'],'filename':r['filename'],'mmff_energy_kcal_mol':r['mmff_energy_kcal_mol'],'atoms':r['atoms']} for r in selected]},indent=2))
obs=[{'name':'sampling','value':{'method':'RDKit ETKDGv3/MMFF94 conformer generation','smiles':'CCCCCC','seed':SEED,'n_generated':len(records),'n_unique':len(retained),'n_selected':len(selected),'force_field_converged_count':sum(1 for r in records if r['force_field_converged']),'search_scope':'48 ETKDGv3 conformers, MMFF94 optimization, finite search','geometry_source':'RDKit from SMILES; prepared.xyz used as initial identity/geometry anchor','prepared_anchor_elements':anchor_elements},'unit':None,'sources':[{'filename':'prepared.xyz','location':'initial identity/geometry anchor'},{'filename':'members.json','location':'all generated member records'}],'formula':None,'artifacts':[r['filename'] for r in records]+['members.json','selected.json'],'limitations':['Finite conformer search, not exhaustive; lowest MMFF member is not a global minimum.','MMFF94 energies are force-field values, not ORCA energies.']},{'name':'sampling_check','value':{'rmsd_threshold_angstrom':THRESH,'atom_set':'six carbon heavy atoms','symmetry_mapping':'identity and carbon-chain reversal','pairwise_selected_rmsd':pair,'duplicate_count':sum(1 for r in records if r['duplicate_of']),'unique_retained_count':len(retained)},'unit':'angstrom','sources':[{'filename':'members.json','location':'RMSD duplicate decisions'}],'formula':'min over identity/reversed-chain Kabsch heavy-atom RMSD','artifacts':['members.json','selected.json'],'limitations':['RMSD uses carbon heavy atoms only, as required for hexane chain reversal equivalence.']},{'name':'member_table','value':records,'unit':None,'sources':[{'filename':'members.json','location':'full candidate member table'}],'formula':None,'artifacts':[r['filename'] for r in records]+['members.json','selected.json'],'limitations':['Includes all 48 generated members with selected/duplicate/not-selected reasons.']}]
Path('observations.json').write_text(json.dumps({'version':1,'observations':obs},indent=2))
print('search_done', len(records), len(retained), len(selected))