"""Windows LPAC capability boundary for private, staged task files.

The ephemeral profile is removed at close; its automatic storage is denied to
the task SID, so it does not create a second writable task tree. The single
registryRead capability is required by the Windows Python loader; it only grants
registry resources explicitly ACL'd for that capability. No network, COM, broad
filesystem or ALL APPLICATION PACKAGES capability is added.
Job limits and process creation policy are applied separately at atomic launch.
"""

from __future__ import annotations

import ctypes as c
import os
import stat
import uuid
from dataclasses import dataclass
from pathlib import Path


def _windows():
    if os.name != "nt":
        raise RuntimeError("LPAC isolation is only implemented on Windows")
    from . import _windows as win
    return win


def _bind(dll, name, result, *args):
    function = getattr(dll, name)
    function.restype = result
    function.argtypes = args
    return function


def _regular_tree(path: Path) -> None:
    """Never grant an ACL through a junction/symlink into unrelated files."""
    if not path.exists():
        raise ValueError(f"Isolation grant target does not exist: {path}")
    for candidate in [path, *path.parents]:
        info = candidate.lstat()
        attributes = info.st_file_attributes
        if attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT:
            raise ValueError("Isolation paths cannot traverse reparse points")
        if stat.S_ISREG(info.st_mode) and info.st_nlink != 1:
            raise ValueError("Isolation files cannot have hard-link aliases")
    if path.is_dir():
        for base, directories, files in os.walk(path, followlinks=False):
            for name in directories + files:
                candidate = Path(base) / name
                info = candidate.lstat()
                if info.st_file_attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT:
                    raise ValueError("Isolation trees cannot contain reparse points")
                if stat.S_ISREG(info.st_mode) and info.st_nlink != 1:
                    raise ValueError("Isolation files cannot have hard-link aliases")


def _grant(path: Path, sid, writable: bool) -> None:
    win = _windows()
    class TRUSTEE(c.Structure):
        _fields_ = [("pMultipleTrustee", win.LPVOID), ("MultipleTrusteeOperation", c.c_int),
                    ("TrusteeForm", c.c_int), ("TrusteeType", c.c_int),
                    ("ptstrName", win.LPVOID)]
    class ACCESS(c.Structure):
        _fields_ = [("grfAccessPermissions", win.w.DWORD), ("grfAccessMode", c.c_int),
                    ("grfInheritance", win.w.DWORD), ("Trustee", TRUSTEE)]

    get_security = _bind(win.advapi32, "GetNamedSecurityInfoW", win.w.DWORD,
                         win.w.LPWSTR, c.c_int, win.w.DWORD, win.LPVOID, win.LPVOID,
                         c.POINTER(win.LPVOID), win.LPVOID, c.POINTER(win.LPVOID))
    set_entries = _bind(win.advapi32, "SetEntriesInAclW", win.w.DWORD, win.w.ULONG,
                        c.POINTER(ACCESS), win.LPVOID, c.POINTER(win.LPVOID))
    set_security = _bind(win.advapi32, "SetNamedSecurityInfoW", win.w.DWORD,
                         win.w.LPWSTR, c.c_int, win.w.DWORD, win.LPVOID, win.LPVOID,
                         win.LPVOID, win.LPVOID)
    local_free = _bind(win.k32, "LocalFree", win.LPVOID, win.LPVOID)
    owner, old_acl, descriptor, new_acl = (win.LPVOID() for _ in range(4))
    def check_status(status, operation):
        if status:
            raise OSError(status, f"{operation}: {c.WinError(status)}")
    try:
        check_status(get_security(str(path), 1, 5, c.byref(owner), None, c.byref(old_acl),
                                  None, c.byref(descriptor)), "GetNamedSecurityInfo")
        # Read/execute; writable tasks additionally get write/delete, never ACL ownership.
        permission = 0x1200A9 | (0x130116 if writable else 0)
        entries = (ACCESS * 3)(
            ACCESS(permission & ~0x10000, 1, 0, TRUSTEE(None, 0, 0, 5, sid)),
            ACCESS(permission, 1, 0xB if path.is_dir() else 0, TRUSTEE(None, 0, 0, 5, sid)),
            # A newly-created directory can inherit only Modify on a data drive.
            # Its owner may change its DACL, but labeling additionally needs WRITE_OWNER.
            ACCESS(0x1F01FF, 1, 3 if path.is_dir() else 0, TRUSTEE(None, 0, 0, 1, owner)),
        )
        check_status(set_entries(3, entries, old_acl, c.byref(new_acl)),
                     "SetEntriesInAcl")
        check_status(set_security(str(path), 1, 4, None, None, new_acl, None),
                     "SetNamedSecurityInfo")
    finally:
        for pointer in (descriptor, new_acl):
            if pointer:
                local_free(pointer)
    if writable:
        # AppContainer is low integrity. Label only the dedicated task write tree.
        convert = _bind(win.advapi32, "ConvertStringSecurityDescriptorToSecurityDescriptorW",
                        win.w.BOOL, win.w.LPCWSTR, win.w.DWORD,
                        c.POINTER(win.LPVOID), win.LPVOID)
        get_sacl = _bind(win.advapi32, "GetSecurityDescriptorSacl", win.w.BOOL,
                         win.LPVOID, c.POINTER(win.w.BOOL), c.POINTER(win.LPVOID),
                         c.POINTER(win.w.BOOL))
        low, sacl = win.LPVOID(), win.LPVOID()
        present, defaulted = win.w.BOOL(), win.w.BOOL()
        try:
            win.check(convert("S:(ML;OICI;NW;;;LW)", 1, c.byref(low), None),
                      "Low integrity descriptor")
            win.check(get_sacl(low, c.byref(present), c.byref(sacl), c.byref(defaulted)),
                      "GetSecurityDescriptorSacl")
            check_status(set_security(str(path), 1, 0x10, None, None, None, sacl),
                         "SetNamedSecurityInfo(low integrity)")
        finally:
            if low:
                local_free(low)


def _deny_profile_storage(path: Path, sid: str) -> None:
    """Replace only this newly-created profile folder's DACL, never a shared path."""
    win = _windows()
    convert = _bind(win.advapi32, "ConvertStringSecurityDescriptorToSecurityDescriptorW",
                    win.w.BOOL, win.w.LPCWSTR, win.w.DWORD,
                    c.POINTER(win.LPVOID), win.LPVOID)
    get_acl = _bind(win.advapi32, "GetSecurityDescriptorDacl", win.w.BOOL,
                    win.LPVOID, c.POINTER(win.w.BOOL), c.POINTER(win.LPVOID),
                    c.POINTER(win.w.BOOL))
    set_security = _bind(win.advapi32, "SetNamedSecurityInfoW", win.w.DWORD,
                         win.w.LPWSTR, c.c_int, win.w.DWORD, win.LPVOID, win.LPVOID,
                         win.LPVOID, win.LPVOID)
    descriptor, acl = win.LPVOID(), win.LPVOID()
    present, defaulted = win.w.BOOL(), win.w.BOOL()
    try:
        # Retain host owner access for deterministic cleanup; deny task access explicitly.
        sddl = f"D:P(D;OICI;FA;;;{sid})(A;OICI;FA;;;OW)(A;OICI;FA;;;SY)"
        win.check(convert(sddl, 1, c.byref(descriptor), None), "Profile deny descriptor")
        win.check(get_acl(descriptor, c.byref(present), c.byref(acl), c.byref(defaulted)),
                  "GetSecurityDescriptorDacl(profile)")
        status = set_security(str(path), 1, 4 | 0x80000000, None, None, acl, None)
        if status:
            raise OSError(status, "Cannot restrict ephemeral profile storage")
    finally:
        if descriptor:
            _bind(win.k32, "LocalFree", win.LPVOID, win.LPVOID)(descriptor)


@dataclass(frozen=True)
class IsolationPolicy:
    session: "AppContainerSession"
    allow_children: bool = False

    def attributes(self, win):
        self.session._require_open()
        capabilities = win.SECURITY_CAPABILITIES(
            self.session._sid, c.cast(self.session._capabilities, win.LPVOID), 1, 0,
        )
        attributes = [(0x20009, capabilities), (0x2000F, win.w.DWORD(1))]
        if not self.allow_children:
            attributes.append((0x2000E, win.w.DWORD(1)))
        return attributes

    def validate_launch(self, executable: Path, workdir: Path) -> None:
        self.session._require_open()
        if not any(executable.is_relative_to(path) or executable == path
                   for path in self.session._read_roots):
            raise ValueError("Isolated executable must be in an explicitly staged read tree")
        if not any(workdir.is_relative_to(path) or workdir == path
                   for path in self.session._write_roots):
            raise ValueError("Isolated cwd must be in a dedicated task write tree")
        _regular_tree(executable)
        _regular_tree(workdir)

    def verify_process(self, win, process) -> dict:
        token = win.HANDLE()
        win.check(win.OpenProcessToken(process, 8 | 2, c.byref(token)), "OpenProcessToken")
        try:
            returned = win.w.DWORD()
            is_container = win.w.DWORD()
            win.check(win.GetTokenInformation(token, 29, c.byref(is_container),
                                              c.sizeof(is_container), c.byref(returned)),
                      "GetTokenInformation(TokenIsAppContainer)")
            if not is_container.value:
                raise RuntimeError("Kernel did not create the required AppContainer token")
            _verify_lpac_access_check(win, token)
        finally:
            win.close(token)
        return {"kind": "windows_lpac", "sid": self.session.sid,
                "profile_name": self.session.name,
                "appcontainer_confirmed": True, "lpac_confirmed": True,
                "network_capabilities": [], "profile_created": True,
                "runtime_capabilities": ["registryRead"],
                "profile_storage_denied": True,
                "child_processes_allowed": self.allow_children,
                "read_roots": [str(path) for path in self.session._read_roots],
                "write_roots": [str(path) for path in self.session._write_roots]}


def _verify_lpac_access_check(win, token):
    """Verify the effective opt-out with the kernel's supported access check.

    Win32 GetTokenInformation class 46 is not supported on all Windows builds.
    Ordinary AppContainers can read this synthetic descriptor; LPAC must fail.
    """
    class MAPPING(c.Structure):
        _fields_ = [(name, win.w.DWORD) for name in ("Read", "Write", "Execute", "All")]
    duplicate = _bind(win.advapi32, "DuplicateToken", win.w.BOOL, win.HANDLE, c.c_int,
                      c.POINTER(win.HANDLE))
    convert = _bind(win.advapi32, "ConvertStringSecurityDescriptorToSecurityDescriptorW",
                    win.w.BOOL, win.w.LPCWSTR, win.w.DWORD,
                    c.POINTER(win.LPVOID), win.LPVOID)
    access_check = _bind(win.advapi32, "AccessCheck", win.w.BOOL, win.LPVOID,
                         win.HANDLE, win.w.DWORD, c.POINTER(MAPPING), win.LPVOID,
                         c.POINTER(win.w.DWORD), c.POINTER(win.w.DWORD),
                         c.POINTER(win.w.BOOL))
    impersonation, descriptor = win.HANDLE(), win.LPVOID()
    try:
        win.check(duplicate(token, 2, c.byref(impersonation)), "DuplicateToken(LPAC check)")
        win.check(convert("O:SYG:SYD:(A;;0x1;;;WD)(A;;0x1;;;AC)", 1,
                          c.byref(descriptor), None), "LPAC access descriptor")
        privileges = c.create_string_buffer(1024)
        length, granted, status = win.w.DWORD(1024), win.w.DWORD(), win.w.BOOL()
        mapping = MAPPING(1, 2, 4, 7)
        win.check(access_check(descriptor, impersonation, 1, c.byref(mapping), privileges,
                               c.byref(length), c.byref(granted), c.byref(status)),
                  "AccessCheck(LPAC ALL_APPLICATION_PACKAGES opt-out)")
        if status.value:
            raise RuntimeError("LPAC unexpectedly retains ALL_APPLICATION_PACKAGES access")
    finally:
        win.close(impersonation)
        if descriptor:
            _bind(win.k32, "LocalFree", win.LPVOID, win.LPVOID)(descriptor)


class AppContainerSession:
    """Own one ephemeral LPAC SID and grant only a private staging root.

    All contents beneath ``root`` must be private copies owned by this attempt.
    Original installations, repositories and input artifacts must never be used.
    ACLs contain a never-reused SID; its profile is deleted when the session closes.
    """

    def __init__(self, root: Path):
        win = _windows()
        self.root = Path(root).absolute()
        _regular_tree(self.root)
        self.root = self.root.resolve()
        self._read_roots: list[Path] = []
        self._write_roots: list[Path] = []
        self._sid = win.LPVOID()
        self._registry_sid = win.LPVOID()
        self.name = f"ORCAAgent.Task.{uuid.uuid4().hex}"
        userenv = c.WinDLL("userenv", use_last_error=True)
        create = _bind(userenv, "CreateAppContainerProfile", c.c_long,
                       win.w.LPCWSTR, win.w.LPCWSTR, win.w.LPCWSTR, win.LPVOID,
                       win.w.DWORD, c.POINTER(win.LPVOID))
        status = create(self.name, self.name, self.name, None, 0, c.byref(self._sid))
        if status < 0:
            raise OSError(status, "Cannot create ephemeral AppContainer profile")
        convert = _bind(win.advapi32, "ConvertSidToStringSidW", win.w.BOOL,
                        win.LPVOID, c.POINTER(win.w.LPWSTR))
        text = win.w.LPWSTR()
        try:
            win.check(convert(self._sid, c.byref(text)), "ConvertSidToStringSid")
            self.sid = text.value
        finally:
            if text:
                _bind(win.k32, "LocalFree", win.LPVOID, win.LPVOID)(c.cast(text, win.LPVOID))
        try:
            class SID_AND_ATTRIBUTES(c.Structure):
                _fields_ = [("Sid", win.LPVOID), ("Attributes", win.w.DWORD)]
            from_string = _bind(win.advapi32, "ConvertStringSidToSidW", win.w.BOOL,
                                win.w.LPCWSTR, c.POINTER(win.LPVOID))
            # Microsoft SandboxSecurityTools' documented registryRead capability SID.
            registry_read = ("S-1-15-3-1024-1065365936-1281604716-3511738428-1654721687-"
                             "432734479-3232135806-4053264122-3456934681")
            win.check(from_string(registry_read, c.byref(self._registry_sid)),
                      "ConvertStringSidToSid(registryRead)")
            self._capabilities = (SID_AND_ATTRIBUTES * 1)(
                SID_AND_ATTRIBUTES(self._registry_sid, 4),
            )
            folder = win.w.LPWSTR()
            get_folder = _bind(userenv, "GetAppContainerFolderPath", c.c_long,
                               win.w.LPCWSTR, c.POINTER(win.w.LPWSTR))
            status = get_folder(self.sid, c.byref(folder))
            if status < 0:
                raise OSError(status, "Cannot locate ephemeral AppContainer storage")
            try:
                self.profile_path = Path(folder.value)
                _deny_profile_storage(self.profile_path, self.sid)
            finally:
                ole = c.WinDLL("ole32")
                _bind(ole, "CoTaskMemFree", None, win.LPVOID)(c.cast(folder, win.LPVOID))
        except Exception:
            self.close()
            raise

    def _require_open(self):
        if not self._sid:
            raise RuntimeError("AppContainer session is closed")

    def _target(self, path: Path) -> Path:
        self._require_open()
        path = Path(path).absolute()
        _regular_tree(path)
        path = path.resolve()
        if path == self.root or not path.is_relative_to(self.root):
            raise ValueError("ACL grants require a child of the private staging root")
        return path

    def grant_read(self, path: Path) -> None:
        path = self._target(path)
        _grant(path, self._sid, False)
        self._read_roots.append(path)

    def grant_write(self, path: Path) -> None:
        path = self._target(path)
        _grant(path, self._sid, True)
        self._write_roots.append(path)

    def policy(self, *, allow_children: bool = False) -> IsolationPolicy:
        self._require_open()
        return IsolationPolicy(self, allow_children)

    def close(self) -> None:
        if self._sid:
            win = _windows()
            delete = _bind(c.WinDLL("userenv"), "DeleteAppContainerProfile", c.c_long,
                           win.w.LPCWSTR)
            status = delete(self.name)
            _bind(win.advapi32, "FreeSid", win.LPVOID, win.LPVOID)(self._sid)
            self._sid = None
            if self._registry_sid:
                _bind(win.k32, "LocalFree", win.LPVOID, win.LPVOID)(self._registry_sid)
                self._registry_sid = None
            if status < 0:
                raise OSError(status, "Cannot delete ephemeral AppContainer profile")

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()


def clean_environment(runtime: Path, workdir: Path) -> dict[str, str]:
    """Construct runtime-only variables; never inherit secrets or editable paths."""
    windows = Path(os.environ.get("SystemRoot", "C:/Windows"))
    return {"SystemRoot": str(windows), "WINDIR": str(windows),
            "PATH": os.pathsep.join((str(runtime), str(windows / "System32"))),
            "ComSpec": str(windows / "System32" / "cmd.exe"),
            "TEMP": str(workdir), "TMP": str(workdir),
            "USERPROFILE": str(workdir), "LOCALAPPDATA": str(workdir), "APPDATA": str(workdir),
            "PYTHONNOUSERSITE": "1", "PYTHONDONTWRITEBYTECODE": "1",
            "OMP_NUM_THREADS": "1", "OPENBLAS_NUM_THREADS": "1",
            "MKL_NUM_THREADS": "1"}
