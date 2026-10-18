"""Bounded reads from an untrusted task, without following filesystem redirects."""

import os
import stat
from pathlib import Path


def regular_path(path: Path, root: Path) -> None:
    path, root = path.absolute(), root.absolute()
    if not path.is_relative_to(root):
        raise ValueError("task path escapes its dedicated directory")
    for candidate in [path, *path.parents]:
        info = candidate.lstat()
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            raise ValueError("task reparse points and symbolic links are forbidden")
        if candidate == root:
            break
    if path.is_file() and path.stat().st_nlink != 1:
        raise ValueError("task hard links are forbidden")


def read_mailbox(path: Path, limit: int) -> bytes:
    """Read the fixed child of a broker-owned directory, denying concurrent mutation.

    The directory ACL must deny the task DELETE/WRITE_DAC/WRITE_OWNER on the
    directory itself. No child-supplied path is accepted here.
    """
    if os.name != "nt":
        raise RuntimeError("isolated mailbox requires the Windows backend")
    import ctypes as c
    import msvcrt

    from . import _windows as win

    create = win.k32.CreateFileW
    create.argtypes = [win.w.LPCWSTR, win.w.DWORD, win.w.DWORD, win.LPVOID,
                       win.w.DWORD, win.w.DWORD, win.HANDLE]
    create.restype = win.HANDLE
    handle = create(str(path), 0x80000000, 1, None, 3, 0x00200000, None)
    if handle == c.c_void_p(-1).value:
        raise c.WinError(c.get_last_error())
    descriptor = None
    try:
        final_name = win.k32.GetFinalPathNameByHandleW
        final_name.argtypes = [win.HANDLE, win.w.LPWSTR, win.w.DWORD, win.w.DWORD]
        final_name.restype = win.w.DWORD
        buffer = c.create_unicode_buffer(32768)
        count = final_name(handle, buffer, len(buffer), 0)
        if not count or count >= len(buffer):
            raise c.WinError(c.get_last_error())
        actual = buffer.value.removeprefix("\\\\?\\")
        # Check the opened object's path before any read. This catches an
        # intermediate junction even when its directory is changed concurrently.
        # The caller captures this absolute path before granting task writes.
        if os.path.normcase(actual) != os.path.normcase(str(path.absolute())):
            raise ValueError("mailbox handle resolves outside its pinned path")
        descriptor = msvcrt.open_osfhandle(handle, os.O_RDONLY | os.O_BINARY | os.O_NOINHERIT)
        handle = None
        info = os.fstat(descriptor)
        if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                or getattr(info, "st_file_attributes", 0) & 0x400 or info.st_size > limit):
            raise ValueError("mailbox must be a bounded regular, unlinked file")
        with os.fdopen(descriptor, "rb") as stream:
            descriptor = None
            data = stream.read(limit + 1)
        if len(data) > limit:
            raise ValueError("mailbox exceeds transport budget")
        return data
    finally:
        if descriptor is not None:
            os.close(descriptor)
        if handle:
            win.close(handle)


def collect_paths(root: Path, *, count: int, depth: int = 8):
    """Enumerate after confirmed termination; fail before traversing redirects."""
    pending = [(root, 0)]
    seen = 0
    while pending:
        directory, level = pending.pop()
        with os.scandir(directory) as entries:
            for entry in entries:
                seen += 1
                if seen > count:
                    raise ValueError("task file/directory enumeration budget exhausted")
                path = Path(entry.path)
                regular_path(path, root)
                if entry.is_dir(follow_symlinks=False):
                    if level >= depth:
                        raise ValueError("task directory depth limit exhausted")
                    pending.append((path, level + 1))
                elif entry.is_file(follow_symlinks=False):
                    yield path
                else:
                    raise ValueError("task output is not a regular file")
