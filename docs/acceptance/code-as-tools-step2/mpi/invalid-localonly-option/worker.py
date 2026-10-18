import ctypes as c, json, os, time
from ctypes import wintypes as w
k=c.WinDLL("kernel32", use_last_error=True); a=c.WinDLL("advapi32", use_last_error=True)
k.GetCurrentProcess.restype=w.HANDLE
a.OpenProcessToken.argtypes=[w.HANDLE,w.DWORD,c.POINTER(w.HANDLE)]
a.GetTokenInformation.argtypes=[w.HANDLE,c.c_int,w.LPVOID,w.DWORD,c.POINTER(w.DWORD)]
k.IsProcessInJob.argtypes=[w.HANDLE,w.HANDLE,c.POINTER(w.BOOL)]
token=w.HANDLE(); a.OpenProcessToken(k.GetCurrentProcess(),8,c.byref(token))
size=w.DWORD(); app=w.DWORD(); a.GetTokenInformation(token,29,c.byref(app),c.sizeof(app),c.byref(size))
inside=w.BOOL(); k.IsProcessInJob(k.GetCurrentProcess(),None,c.byref(inside))
print(json.dumps({"pid":os.getpid(),"ppid":os.getppid(),"appcontainer":bool(app.value),"inside_job":bool(inside.value)}),flush=True)
time.sleep(0.4)
