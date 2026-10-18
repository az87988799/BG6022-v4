from pathlib import Path
import json
import os
import psutil

from orca_agent.backends.isolation import AppContainerSession, clean_environment
from orca_agent.backends.local import run_managed

root=Path('E:/orca-agent-isolation-probes/mpi-01a7679ee7e04899b8990fafcc0fbca1')
runtime=root/'runtime'
worker='''import ctypes as c, json, os
print(json.dumps({"pid":os.getpid(),"stage":"before_load"}), flush=True)
m=c.WinDLL("../runtime/msmpi.dll")
m.MPI_Init.argtypes=[c.c_void_p,c.c_void_p];m.MPI_Init.restype=c.c_int
m.MPI_Comm_rank.argtypes=[c.c_int,c.POINTER(c.c_int)];m.MPI_Comm_rank.restype=c.c_int
m.MPI_Comm_size.argtypes=[c.c_int,c.POINTER(c.c_int)];m.MPI_Comm_size.restype=c.c_int
m.MPI_Allreduce.argtypes=[c.c_void_p,c.c_void_p,c.c_int,c.c_int,c.c_int,c.c_int];m.MPI_Allreduce.restype=c.c_int
m.MPI_Finalize.argtypes=[];m.MPI_Finalize.restype=c.c_int
print(json.dumps({"pid":os.getpid(),"stage":"before_init"}), flush=True)
rc=m.MPI_Init(None,None)
print(json.dumps({"pid":os.getpid(),"stage":"after_init","rc":rc}), flush=True)
if rc: raise RuntimeError("MPI_Init failed")
rank=c.c_int();size=c.c_int();m.MPI_Comm_rank(0x44000000,c.byref(rank));m.MPI_Comm_size(0x44000000,c.byref(size))
send=c.c_int(rank.value+1);recv=c.c_int()
rc=m.MPI_Allreduce(c.byref(send),c.byref(recv),1,0x4c000405,0x58000003,0x44000000)
print(json.dumps({"pid":os.getpid(),"stage":"allreduce","rc":rc,"rank":rank.value,"size":size.value,"sum":recv.value}), flush=True)
rc=m.MPI_Finalize()
print(json.dumps({"pid":os.getpid(),"stage":"finalize","rc":rc}), flush=True)
'''

results=[]
for name, settings in [('default',{}),('shm_only',{'MSMPI_DISABLE_SOCK':'1','MSMPI_DISABLE_ND':'1','MSMPI_DISABLE_SHM':'0'})]:
    work=root/('init-'+name);work.mkdir();(work/'worker.py').write_text(worker)
    with AppContainerSession(root) as boundary:
        boundary.grant_read(runtime);boundary.grant_write(work)
        env=clean_environment(runtime,work);env.update(settings)
        args=['-n','2']
        for key,value in settings.items():args.extend(['-genv',key,value])
        args.extend(['../runtime/python.exe','-I','worker.py'])
        outcome=run_managed(runtime/'mpiexec.exe',args,work,cores=2,total_memory_mb=256,timeout_s=20,
            isolation=boundary.policy(allow_children=True),environment=env,argv0='../runtime/mpiexec.exe')
    remaining=[{'pid':p.pid,**p.info} for p in psutil.process_iter(['name','exe','create_time'])
               if (p.info['name'] or '').lower() in {'mpiexec.exe','smpd.exe','msmpilaunchsvc.exe'}]
    record={'configuration':name,'environment':settings,'outcome':outcome,'remaining_mpi_processes':remaining,
        'stdout':(work/'stdout.out').read_text(errors='replace'),'stderr':(work/'stderr.txt').read_text(errors='replace')}
    results.append(record);(root/'mpi-init-results.json').write_text(json.dumps(results,indent=2),encoding='utf-8')
    print(json.dumps(record,indent=2),flush=True)
    if remaining:break
print('EVIDENCE',root/'mpi-init-results.json')
