import ctypes as c, json, os
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
