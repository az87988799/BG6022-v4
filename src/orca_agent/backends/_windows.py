"""Small Win32 binding: atomically job-assigned processes, never shell execution.

Keep the native binding separate from lifecycle policy so unsupported systems
can import the package and report that execution has not been validated.
"""

import ctypes as c
from ctypes import wintypes as w

SIZE_T = c.c_size_t
HANDLE = w.HANDLE
LPVOID = w.LPVOID


class BASIC_LIMIT(c.Structure):
    _fields_ = [
        ("PerProcessUserTimeLimit", c.c_longlong),
        ("PerJobUserTimeLimit", c.c_longlong),
        ("LimitFlags", w.DWORD),
        ("MinimumWorkingSetSize", SIZE_T),
        ("MaximumWorkingSetSize", SIZE_T),
        ("ActiveProcessLimit", w.DWORD),
        ("Affinity", SIZE_T),
        ("PriorityClass", w.DWORD),
        ("SchedulingClass", w.DWORD),
    ]


class IO_COUNTERS(c.Structure):
    _fields_ = [(name, c.c_ulonglong) for name in (
        "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
        "ReadTransferCount", "WriteTransferCount", "OtherTransferCount",
    )]


class EXTENDED_LIMIT(c.Structure):
    _fields_ = [
        ("BasicLimitInformation", BASIC_LIMIT), ("IoInfo", IO_COUNTERS),
        ("ProcessMemoryLimit", SIZE_T), ("JobMemoryLimit", SIZE_T),
        ("PeakProcessMemoryUsed", SIZE_T), ("PeakJobMemoryUsed", SIZE_T),
    ]


class ACCOUNTING(c.Structure):
    _fields_ = [
        ("TotalUserTime", c.c_longlong), ("TotalKernelTime", c.c_longlong),
        ("ThisPeriodTotalUserTime", c.c_longlong),
        ("ThisPeriodTotalKernelTime", c.c_longlong),
        ("TotalPageFaultCount", w.DWORD), ("TotalProcesses", w.DWORD),
        ("ActiveProcesses", w.DWORD), ("TotalTerminatedProcesses", w.DWORD),
    ]


class STARTUPINFO(c.Structure):
    _fields_ = [
        ("cb", w.DWORD), ("lpReserved", w.LPWSTR), ("lpDesktop", w.LPWSTR),
        ("lpTitle", w.LPWSTR), ("dwX", w.DWORD), ("dwY", w.DWORD),
        ("dwXSize", w.DWORD), ("dwYSize", w.DWORD), ("dwXCountChars", w.DWORD),
        ("dwYCountChars", w.DWORD), ("dwFillAttribute", w.DWORD),
        ("dwFlags", w.DWORD), ("wShowWindow", w.WORD),
        ("cbReserved2", w.WORD), ("lpReserved2", LPVOID),
        ("hStdInput", HANDLE), ("hStdOutput", HANDLE), ("hStdError", HANDLE),
    ]


class STARTUPINFOEX(c.Structure):
    _fields_ = [("StartupInfo", STARTUPINFO), ("lpAttributeList", LPVOID)]


class PROCESS_INFORMATION(c.Structure):
    _fields_ = [
        ("hProcess", HANDLE), ("hThread", HANDLE),
        ("dwProcessId", w.DWORD), ("dwThreadId", w.DWORD),
    ]


class COMPLETION_PORT(c.Structure):
    _fields_ = [("CompletionKey", LPVOID), ("CompletionPort", HANDLE)]


k32 = c.WinDLL("kernel32", use_last_error=True)


def _bind(name, result, *args):
    function = getattr(k32, name)
    function.restype = result
    function.argtypes = args
    return function


CreateJobObject = _bind("CreateJobObjectW", HANDLE, LPVOID, w.LPCWSTR)
OpenJobObject = _bind("OpenJobObjectW", HANDLE, w.DWORD, w.BOOL, w.LPCWSTR)
SetInformationJobObject = _bind(
    "SetInformationJobObject", w.BOOL, HANDLE, c.c_int, LPVOID, w.DWORD,
)
QueryInformationJobObject = _bind(
    "QueryInformationJobObject", w.BOOL, HANDLE, c.c_int, LPVOID, w.DWORD, LPVOID,
)
TerminateJobObject = _bind("TerminateJobObject", w.BOOL, HANDLE, w.UINT)
CloseHandle = _bind("CloseHandle", w.BOOL, HANDLE)
SetHandleInformation = _bind("SetHandleInformation", w.BOOL, HANDLE, w.DWORD, w.DWORD)
GetHandleInformation = _bind("GetHandleInformation", w.BOOL, HANDLE, c.POINTER(w.DWORD))
GetCurrentProcess = _bind("GetCurrentProcess", HANDLE)
GetProcessAffinityMask = _bind(
    "GetProcessAffinityMask", w.BOOL, HANDLE, c.POINTER(SIZE_T), c.POINTER(SIZE_T),
)
GetActiveProcessorGroupCount = _bind("GetActiveProcessorGroupCount", w.WORD)
InitializeAttributes = _bind(
    "InitializeProcThreadAttributeList", w.BOOL, LPVOID, w.DWORD, w.DWORD,
    c.POINTER(SIZE_T),
)
UpdateAttribute = _bind(
    "UpdateProcThreadAttribute", w.BOOL, LPVOID, w.DWORD, SIZE_T, LPVOID, SIZE_T,
    LPVOID, LPVOID,
)
DeleteAttributes = _bind("DeleteProcThreadAttributeList", None, LPVOID)
CreateProcess = _bind(
    "CreateProcessW", w.BOOL, w.LPCWSTR, w.LPWSTR, LPVOID, LPVOID, w.BOOL,
    w.DWORD, LPVOID, w.LPCWSTR, c.POINTER(STARTUPINFOEX),
    c.POINTER(PROCESS_INFORMATION),
)
ResumeThread = _bind("ResumeThread", w.DWORD, HANDLE)
GetProcessTimes = _bind(
    "GetProcessTimes", w.BOOL, HANDLE, c.POINTER(w.FILETIME),
    c.POINTER(w.FILETIME), c.POINTER(w.FILETIME), c.POINTER(w.FILETIME),
)
GetExitCodeProcess = _bind("GetExitCodeProcess", w.BOOL, HANDLE, c.POINTER(w.DWORD))
OpenProcess = _bind("OpenProcess", HANDLE, w.DWORD, w.BOOL, w.DWORD)
IsProcessInJob = _bind("IsProcessInJob", w.BOOL, HANDLE, HANDLE, c.POINTER(w.BOOL))
QueryFullProcessImageName = _bind(
    "QueryFullProcessImageNameW", w.BOOL, HANDLE, w.DWORD, w.LPWSTR, c.POINTER(w.DWORD),
)
CreateIoCompletionPort = _bind(
    "CreateIoCompletionPort", HANDLE, HANDLE, HANDLE, SIZE_T, w.DWORD,
)
GetQueuedCompletionStatus = _bind(
    "GetQueuedCompletionStatus", w.BOOL, HANDLE, c.POINTER(w.DWORD),
    c.POINTER(SIZE_T), c.POINTER(LPVOID), w.DWORD,
)


def check(ok, operation):
    if not ok:
        raise OSError(c.get_last_error(), f"{operation}: {c.WinError(c.get_last_error())}")
    return ok


def close(handle):
    if handle:
        CloseHandle(handle)


def creation_time(process):
    times = [w.FILETIME() for _ in range(4)]
    check(GetProcessTimes(process, *(c.byref(item) for item in times)), "GetProcessTimes")
    ticks = (times[0].dwHighDateTime << 32) | times[0].dwLowDateTime
    return ticks, ticks / 10_000_000 - 11_644_473_600


def accounting(job):
    value = ACCOUNTING()
    check(QueryInformationJobObject(job, 1, c.byref(value), c.sizeof(value), None),
          "QueryInformationJobObject(accounting)")
    return value


def limits(job):
    value = EXTENDED_LIMIT()
    check(QueryInformationJobObject(job, 9, c.byref(value), c.sizeof(value), None),
          "QueryInformationJobObject(limits)")
    return value


def process_ids(job):
    """Ask the Job itself, including ranks created by MPI service processes."""
    capacity = 64
    while capacity <= 4096:
        class PROCESS_LIST(c.Structure):
            _fields_ = [("NumberOfAssignedProcesses", w.DWORD),
                        ("NumberOfProcessIdsInList", w.DWORD),
                        ("ProcessIdList", SIZE_T * capacity)]

        value = PROCESS_LIST()
        if QueryInformationJobObject(job, 3, c.byref(value), c.sizeof(value), None):
            return list(value.ProcessIdList[:value.NumberOfProcessIdsInList])
        if c.get_last_error() != 234:  # ERROR_MORE_DATA: the process list grew.
            check(False, "QueryInformationJobObject(process list)")
        capacity *= 2
    raise RuntimeError("Job process evidence exceeds the bounded 4096-process sample")
