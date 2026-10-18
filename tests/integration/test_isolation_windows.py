"""Real LPAC/Job tests: child code is not monkeypatched or mocked."""

import json
import os
import subprocess
import time
import uuid
from pathlib import Path

import pytest

from orca_agent.backends.isolation import AppContainerSession, IsolationPolicy, clean_environment
from orca_agent.backends.local import check_active_job, run_managed, sample_job_processes
from orca_agent.backends.runtime_files import stage_python_runtime

pytestmark = [pytest.mark.backend, pytest.mark.skipif(os.name != "nt", reason="Windows only")]


@pytest.fixture(scope="module")
def staged(tmp_path_factory):
    root = tmp_path_factory.mktemp("lpac")
    sdk = Path(__file__).resolve().parents[2] / "src/orca_agent/task_sdk.py"
    runtime = root / "runtime"
    stage_python_runtime(runtime, sdk)
    return root, runtime


def launch(staged, code, *, readonly=(), writable=(), environment=None, **options):
    root, runtime = staged
    work = root / f"work-{uuid.uuid4().hex}"
    work.mkdir()
    with AppContainerSession(root) as boundary:
        boundary.grant_read(runtime)
        for path in readonly:
            boundary.grant_read(path)
        for path in writable:
            boundary.grant_write(path)
        boundary.grant_write(work)
        code = code.replace("PROFILE_CANARY", repr(str(boundary.profile_path)))
        result = run_managed(runtime / "python.exe", ["-I", "-S", "-c", code], work,
                             isolation=boundary.policy(),
                             environment=environment or clean_environment(runtime, work), **options)
        profile_path = boundary.profile_path
    assert not profile_path.exists(), "ephemeral profile storage was not removed"
    work.with_suffix(".execution.json").write_text(
        json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8",
    )
    return result, work


def test_lpac_python_scientific_libraries_and_explicit_input(staged):
    root, _ = staged
    inputs = root / "inputs"
    inputs.mkdir()
    (inputs / "numbers.json").write_text("[2,3,5]")
    code = f"""
import json,pathlib,numpy,opi
from rdkit import Chem
data=json.loads(pathlib.Path({str(inputs / 'numbers.json')!r}).read_text())
value={{'sum':int(numpy.sum(data)), 'atoms':Chem.MolFromSmiles('CCO').GetNumAtoms()}}
pathlib.Path('result.json').write_text(json.dumps(value))
print(json.dumps(value))
"""
    result, work = launch(staged, code, readonly=[inputs])
    assert result["state"] == "completed", (result, (work / "stderr.txt").read_text())
    assert json.loads((work / "result.json").read_text()) == {"sum": 10, "atoms": 3}
    assert result["isolation"]["lpac_confirmed"]
    assert result["handle"]["isolation"] == result["isolation"]
    assert result["isolation"]["profile_name"].startswith("ORCAAgent.Task.")
    assert result["resource_usage"]["total_processes"] == 1


def test_lpac_real_file_registry_network_child_and_parent_boundaries(staged, monkeypatch):
    import msvcrt
    import winreg

    root, runtime = staged
    secret = root / "credential-canary.txt"
    secret.write_text("fake-secret-never-read")
    immutable = root / "immutable.txt"
    immutable.write_text("fixed input")
    reparse_mailbox = root / "reparse-mailbox"
    reparse_mailbox.mkdir()
    registry_path = rf"Software\ORCAAgentIsolationCanary\{uuid.uuid4().hex}"
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, registry_path) as key:
        winreg.SetValueEx(key, "credential", 0, winreg.REG_SZ, "fake-registry-secret")
    monkeypatch.setenv("ORCA_FAKE_SECRET", "fake-environment-secret")
    source = Path(__file__).resolve().parents[2] / "src/orca_agent/store.py"
    canary_stream = secret.open("rb")
    canary_handle = msvcrt.get_osfhandle(canary_stream.fileno())
    os.set_handle_inheritable(canary_handle, True)
    code = f"""
import ctypes,json,os,pathlib,socket,struct,subprocess,sys,winreg
from ctypes import wintypes as w
report={{}}
def attempt(name,fn):
    try: fn(); report[name]={{'blocked':False}}
    except OSError as error: report[name]={{'blocked':True,'winerror':getattr(error,'winerror',None),'errno':error.errno}}
attempt('credential_file', lambda:pathlib.Path({str(secret)!r}).read_bytes())
attempt('product_source', lambda:pathlib.Path({str(source)!r}).read_bytes())
attempt('input_write', lambda:pathlib.Path({str(immutable)!r}).write_text('modified'))
attempt('runtime_write', lambda:pathlib.Path({str(runtime / 'python311._pth')!r}).write_text('changed'))
attempt('profile_storage', lambda:list(pathlib.Path(PROFILE_CANARY).iterdir()))
attempt('delete_work_root', lambda:pathlib.Path.cwd().rename(pathlib.Path.cwd().with_name('renamed')))
attempt('registry_credential', lambda:winreg.OpenKey(winreg.HKEY_CURRENT_USER,{registry_path!r}))
attempt('child_python', lambda:subprocess.run([sys.executable,'-c','print(123)'],check=True))
attempt('child_shell', lambda:subprocess.run([os.environ['ComSpec'],'/c','echo forbidden'],check=True))
for name,address in [('network_loopback',('127.0.0.1',443)),('network_external',('192.0.2.1',443))]:
    def connect(address=address):
        with socket.socket() as sock: sock.settimeout(0.5); sock.connect(address)
    attempt(name,connect)
kernel=ctypes.WinDLL('kernel32',use_last_error=True)
kernel.CreateFileW.argtypes=[w.LPCWSTR,w.DWORD,w.DWORD,w.LPVOID,w.DWORD,w.DWORD,w.HANDLE]
kernel.CreateFileW.restype=w.HANDLE
kernel.CloseHandle.argtypes=[w.HANDLE]
directory=kernel.CreateFileW({str(reparse_mailbox)!r},0x40000000,7,None,3,0x02200000,None)
assert directory!=w.HANDLE(-1).value
target={str(root)!r}
sub=(chr(92)+'??'+chr(92)+target).encode('utf-16-le');pr=target.encode('utf-16-le')
body=struct.pack('<HHHH',0,len(sub),len(sub)+2,len(pr))+sub+bytes(2)+pr+bytes(2)
data=struct.pack('<IHH',0xA0000003,len(body),0)+body
buffer=ctypes.create_string_buffer(data);returned=w.DWORD()
kernel.DeviceIoControl.argtypes=[w.HANDLE,w.DWORD,w.LPVOID,w.DWORD,w.LPVOID,w.DWORD,ctypes.POINTER(w.DWORD),w.LPVOID]
kernel.DeviceIoControl.restype=w.BOOL
changed=kernel.DeviceIoControl(directory,0x900A4,buffer,len(data),None,0,ctypes.byref(returned),None)
report['root_junction_mutation']={{'blocked':not bool(changed),'winerror':ctypes.get_last_error()}}
kernel.CloseHandle(directory)
kernel.CreateProcessW.argtypes=[w.LPCWSTR,w.LPWSTR,w.LPVOID,w.LPVOID,w.BOOL,w.DWORD,w.LPVOID,w.LPCWSTR,w.LPVOID,w.LPVOID]
kernel.CreateProcessW.restype=w.BOOL
startup=ctypes.create_string_buffer(104);w.DWORD.from_buffer(startup).value=104
process=ctypes.create_string_buffer(24)
command=ctypes.create_unicode_buffer('"'+sys.executable+'" -c "print(1)"')
created=kernel.CreateProcessW(sys.executable,command,None,None,False,8,None,None,startup,process)
report['native_child_bypass']={{'blocked':not bool(created),'winerror':ctypes.get_last_error()}}
kernel.ReadFile.argtypes=[w.HANDLE,w.LPVOID,w.DWORD,ctypes.POINTER(w.DWORD),w.LPVOID]
kernel.ReadFile.restype=w.BOOL
buf=ctypes.create_string_buffer(32);count=w.DWORD()
try:
    read=kernel.ReadFile({canary_handle},buf,32,ctypes.byref(count),None)
    report['inheritable_canary_handle']={{'blocked':not bool(read),'winerror':ctypes.get_last_error()}}
except OSError as error:
    report['inheritable_canary_handle']={{'blocked':True,'winerror':error.winerror}}
kernel.OpenProcess.argtypes=[w.DWORD,w.BOOL,w.DWORD];kernel.OpenProcess.restype=w.HANDLE
handle=kernel.OpenProcess(0x1FFFFF,False,{os.getpid()})
report['parent_process']={{'blocked':not bool(handle),'winerror':ctypes.get_last_error()}}
if handle: kernel.CloseHandle(handle)
report['secret_environment']={{'blocked':'ORCA_FAKE_SECRET' not in os.environ}}
print(json.dumps(report))
"""
    try:
        result, work = launch(staged, code, readonly=[immutable], writable=[reparse_mailbox])
    finally:
        winreg.DeleteKey(winreg.HKEY_CURRENT_USER, registry_path)
        os.set_handle_inheritable(canary_handle, False)
        canary_stream.close()
        reparse_mailbox.rmdir()  # Also safely removes a junction if a regression created one.
    assert result["state"] == "completed", (result, (work / "stderr.txt").read_text())
    report = json.loads((work / "stdout.out").read_text())
    assert all(item["blocked"] for item in report.values()), report
    assert report["network_loopback"]["winerror"] == 10013, report
    assert report["network_external"]["winerror"] == 10013, report
    assert report["root_junction_mutation"]["winerror"] == 5, report
    assert immutable.read_text() == "fixed input"
    assert result["resource_usage"]["total_processes"] == 1


@pytest.mark.parametrize("mode", ["timeout", "cancel", "memory"])
def test_lpac_real_limits(staged, mode):
    start = time.monotonic()
    code = "import time; time.sleep(10)"
    options = {"timeout_s": 0.7} if mode == "timeout" else {"timeout_s": 10}
    if mode == "cancel":
        options["cancel_requested"] = lambda: time.monotonic() - start > 0.7
    if mode == "memory":
        options["total_memory_mb"] = 64
        code = "import time; data=bytearray(128*1024*1024); time.sleep(10)"
    result, _ = launch(staged, code, **options)
    assert result["state"] == {"timeout": "timed_out", "cancel": "cancelled", "memory": "failed"}[mode]
    if mode == "memory":
        assert result["reason"] == "job_memory_limit_exceeded", result
    assert result["resource_usage"]["active_processes"] == 0


@pytest.mark.parametrize("mode", ["timeout", "cancel"])
def test_lpac_controls_remain_responsive_during_coordinator_work(staged, mode):
    work_started = None
    confirmed = []

    def coordinator_work(handle):
        nonlocal work_started
        work_started = time.monotonic()
        # Represents chunked artifact/hash work after a brokered child returns,
        # while the outer process is still waiting for its SDK response.
        while time.monotonic() - work_started < 3:
            try:
                check_active_job(handle["job_name"])
            except RuntimeError as error:
                confirmed.append((str(error), sample_job_processes(handle["job_name"])))
                raise
            time.sleep(0.01)
        raise AssertionError("parent control did not interrupt bounded coordinator work")

    options = {"timeout_s": 0.5 if mode == "timeout" else 5}
    if mode == "cancel":
        options["cancel_requested"] = lambda: (
            work_started is not None and time.monotonic() - work_started >= 0.2
        )
    result, _ = launch(staged, "import time;time.sleep(10)",
                       on_poll=coordinator_work, **options)
    expected_state, expected_reason = (
        ("timed_out", "deadline_exceeded") if mode == "timeout"
        else ("cancelled", "cancel_requested")
    )
    assert work_started is not None
    assert confirmed and confirmed[0][0] == (
        f"managed_parent_control: {expected_state}: {expected_reason}"
    ), (result, confirmed)
    assert confirmed[0][1]["active_processes"] == 0
    assert result["state"] == expected_state, result
    assert result["reason"] == expected_reason, result
    assert result["resource_usage"]["active_processes"] == 0
    assert result["resource_usage"]["total_processes"] == 1
    with pytest.raises(RuntimeError, match="not active"):
        check_active_job(result["handle"]["job_name"])


def test_isolation_rejects_hardlink_before_acl_grant(staged):
    root, _ = staged
    original = root / "hardlink-original.txt"
    original.write_text("immutable")
    linked = root / "hardlink-alias.txt"
    os.link(original, linked)
    try:
        with pytest.raises(ValueError, match="hard-link"):
            AppContainerSession(root)
    finally:
        linked.unlink()


def test_lpac_nested_job_aggregates_python_and_brokered_process(staged):
    root, runtime = staged
    nested = []
    child_dir = root / f"nested-{uuid.uuid4().hex}"
    child_dir.mkdir()
    with AppContainerSession(root) as child_boundary:
        child_boundary.grant_read(runtime)
        child_boundary.grant_write(child_dir)
        def poll(handle):
            if nested:
                return
            nested.append(run_managed(
                runtime / "python.exe", ["-I", "-S", "-c", "print('brokered')"], child_dir,
                parent_job_name=handle["job_name"], isolation=child_boundary.policy(),
                environment=clean_environment(runtime, child_dir),
            ))
        result, _ = launch(staged, "import time; time.sleep(1)", on_poll=poll)
    assert nested[0]["state"] == "completed", nested
    assert result["state"] == "completed", result
    assert result["resource_usage"]["total_processes"] == 2
    assert nested[0]["handle"]["parent_job_name"] == result["handle"]["job_name"]


def test_lpac_parent_memory_limit_covers_brokered_child(staged):
    root, runtime = staged
    nested = []
    child_dir = root / f"nested-memory-{uuid.uuid4().hex}"
    child_dir.mkdir()
    with AppContainerSession(root) as child_boundary:
        child_boundary.grant_read(runtime)
        child_boundary.grant_write(child_dir)
        def poll(handle):
            if nested or not (Path(handle["workdir"]) / "ready").exists():
                return
            nested.append(run_managed(
                runtime / "python.exe",
                ["-I", "-S", "-c", "data=bytearray(80*1024*1024)"], child_dir,
                total_memory_mb=128, parent_job_name=handle["job_name"],
                isolation=child_boundary.policy(), environment=clean_environment(runtime, child_dir),
            ))
        result, _ = launch(
            staged, "import time,pathlib; data=bytearray(80*1024*1024); "
            "pathlib.Path('ready').write_text(''); time.sleep(10)",
            total_memory_mb=128, timeout_s=5, on_poll=poll,
        )
    assert len(nested) == 1
    assert result["reason"] == "job_memory_limit_exceeded", (result, nested)
    assert result["resource_usage"]["active_processes"] == 0
    assert result["resource_usage"]["total_processes"] == 2


def test_lpac_permitted_native_children_inherit_appcontainer(staged):
    root, runtime = staged
    work = root / f"native-child-{uuid.uuid4().hex}"
    work.mkdir()
    child = """
import ctypes,json,os,pathlib,time
from ctypes import wintypes as w
k=ctypes.WinDLL('kernel32');a=ctypes.WinDLL('advapi32')
k.GetCurrentProcess.restype=w.HANDLE
a.OpenProcessToken.argtypes=[w.HANDLE,w.DWORD,ctypes.POINTER(w.HANDLE)]
a.GetTokenInformation.argtypes=[w.HANDLE,ctypes.c_int,w.LPVOID,w.DWORD,ctypes.POINTER(w.DWORD)]
token=w.HANDLE();assert a.OpenProcessToken(k.GetCurrentProcess(),8,ctypes.byref(token))
value=w.DWORD();size=w.DWORD()
assert a.GetTokenInformation(token,29,ctypes.byref(value),4,ctypes.byref(size))
pathlib.Path('child-token.json').write_text(json.dumps({'appcontainer':value.value,'pid':os.getpid()}))
time.sleep(1)
"""
    code = (f"import sys,subprocess;subprocess.run([sys.executable,'-I','-S','-c',{child!r}],"
            "check=True,creationflags=8)")
    descendant_isolation = []
    with AppContainerSession(root) as boundary:
        boundary.grant_read(runtime)
        boundary.grant_write(work)
        def check_descendant(_):
            from orca_agent.backends import _windows as win
            path = work / "child-token.json"
            if descendant_isolation or not path.exists():
                return
            try:
                pid = json.loads(path.read_text())["pid"]
            except json.JSONDecodeError:
                return  # Next bounded poll reads the small complete write.
            process = win.check(win.OpenProcess(0x1000, False, pid), "OpenProcess(child proof)")
            try:
                descendant_isolation.append(boundary.policy(allow_children=True).verify_process(win, process))
            finally:
                win.close(process)
        result = run_managed(runtime / "python.exe", ["-I", "-S", "-c", code], work,
                             isolation=boundary.policy(allow_children=True),
                             environment=clean_environment(runtime, work), on_poll=check_descendant)
    assert result["state"] == "completed", (result, (work / "stderr.txt").read_text())
    assert json.loads((work / "child-token.json").read_text())["appcontainer"] == 1
    assert result["resource_usage"]["total_processes"] == 3  # Parent, hidden console, child.
    assert descendant_isolation and descendant_isolation[0]["lpac_confirmed"]


def test_lpac_native_console_preserves_helper_stdout_and_stderr(staged):
    root, runtime = staged
    work = root / f"native-streams-{uuid.uuid4().hex}"
    work.mkdir()
    (work / "helper.py").write_text(
        "import sys;print('native-helper-out');print('native-helper-err',file=sys.stderr)"
    )
    code = """
import os,pathlib,subprocess,sys
print('parent-out',flush=True)
command=os.path.relpath(sys.executable,pathlib.Path.cwd())+' -I -S helper.py'
subprocess.run([os.environ['ComSpec'],'/d','/c',command],check=True)
"""
    with AppContainerSession(root) as boundary:
        boundary.grant_read(runtime)
        boundary.grant_write(work)
        result = run_managed(runtime / "python.exe", ["-I", "-S", "-c", code], work,
                             isolation=boundary.policy(allow_children=True), timeout_s=5,
                             environment=clean_environment(runtime, work))
    assert result["state"] == "completed", result
    assert (work / "stdout.out").read_text().splitlines() == ["parent-out", "native-helper-out"]
    assert (work / "stderr.txt").read_text().strip() == "native-helper-err"
    assert result["resource_usage"]["active_processes"] == 0


def test_isolation_rejects_junction_before_acl_grant(staged):
    root, _ = staged
    target = root / "junction-target"
    target.mkdir()
    link = root / "junction-alias"
    result = subprocess.run([os.environ["ComSpec"], "/d", "/c", "mklink", "/J", str(link),
                             str(target)], capture_output=True)
    assert result.returncode == 0, result.stderr
    try:
        with pytest.raises(ValueError, match="reparse"):
            AppContainerSession(root)
    finally:
        link.rmdir()  # Windows removes the junction itself, not the target tree.
    assert target.is_dir()


def test_lpac_relative_executable_name_preserves_native_helper_access(staged):
    root, runtime = staged
    work = root / f"relative-argv0-{uuid.uuid4().hex}"
    work.mkdir()
    code = """
import json,os,pathlib,subprocess,sys
relative=os.path.relpath(sys.executable,pathlib.Path.cwd())
output=subprocess.run([os.environ['ComSpec'],'/d','/c',relative+' -V'],capture_output=True)
pathlib.Path('helper.json').write_text(json.dumps({'exit':output.returncode,'stdout':output.stdout.decode()}))
"""
    with AppContainerSession(root) as boundary:
        boundary.grant_read(runtime)
        boundary.grant_write(work)
        result = run_managed(runtime / "python.exe", ["-I", "-S", "-c", code], work,
                             argv0=os.path.relpath(runtime / "python.exe", work),
                             isolation=boundary.policy(allow_children=True),
                             environment=clean_environment(runtime, work))
    assert result["state"] == "completed", result
    helper = json.loads((work / "helper.json").read_text())
    assert helper["exit"] == 0, helper
    assert helper["stdout"].startswith("Python ")


def test_ordinary_appcontainer_is_rejected_before_code_resumes(staged):
    root, runtime = staged
    work = root / f"ordinary-container-{uuid.uuid4().hex}"
    work.mkdir()
    class OrdinaryContainer(IsolationPolicy):
        def attributes(self, win):
            return [(key, value) for key, value in super().attributes(win) if key != 0x2000F]
    with AppContainerSession(root) as boundary:
        boundary.grant_read(runtime)
        boundary.grant_write(work)
        result = run_managed(runtime / "python.exe", ["-I", "-S", "-c", "print('not executed')"],
                             work, isolation=OrdinaryContainer(boundary),
                             environment=clean_environment(runtime, work))
    assert result["state"] == "failed", result
    assert "retains ALL_APPLICATION_PACKAGES" in result["reason"], result
    assert not (work / "stdout.out").read_bytes()
