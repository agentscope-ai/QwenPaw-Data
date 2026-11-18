"""Pin a workspace file handle before reading its contents."""

from __future__ import annotations

import os
import stat
from pathlib import Path
from typing import BinaryIO


def _open_posix(root: Path, relative: Path) -> BinaryIO:
    # Walk from the filesystem root so replacing any directory with a symlink
    # after Path.resolve() cannot redirect the eventual open.
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    directory = os.open(root.anchor, flags)
    try:
        for part in (*root.parts[1:], *relative.parts[:-1]):
            child = os.open(part, flags, dir_fd=directory)
            os.close(directory)
            directory = child
        fd = os.open(
            relative.parts[-1],
            os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
            dir_fd=directory,
        )
    finally:
        os.close(directory)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise OSError("workspace file is not a regular file")
        return os.fdopen(fd, "rb")
    except BaseException:
        os.close(fd)
        raise


def _open_windows(root: Path, candidate: Path) -> BinaryIO:
    # Open both handles before checking their final targets. Subsequent renames
    # cannot change the opened file, and junctions resolve in the handle paths.
    import ctypes
    import msvcrt
    from ctypes import wintypes

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateFileW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    ]
    kernel.CreateFileW.restype = wintypes.HANDLE
    kernel.GetFinalPathNameByHandleW.argtypes = [
        wintypes.HANDLE,
        wintypes.LPWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
    ]
    kernel.GetFinalPathNameByHandleW.restype = wintypes.DWORD
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    root_handle = kernel.CreateFileW(str(root), 0x80, 7, None, 3, 0x02000000, None)
    if root_handle == wintypes.HANDLE(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())

    def final_name(handle: int) -> str:
        needed = kernel.GetFinalPathNameByHandleW(handle, None, 0, 0)
        if not needed:
            raise ctypes.WinError(ctypes.get_last_error())
        buffer = ctypes.create_unicode_buffer(needed + 1)
        if not kernel.GetFinalPathNameByHandleW(handle, buffer, len(buffer), 0):
            raise ctypes.WinError(ctypes.get_last_error())
        value = buffer.value
        if value.startswith("\\\\?\\UNC\\"):
            value = "\\\\" + value[8:]
        elif value.startswith("\\\\?\\"):
            value = value[4:]
        return os.path.normcase(os.path.normpath(value))

    try:
        fd = os.open(candidate, os.O_RDONLY | os.O_BINARY)
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise OSError("workspace file is not a regular file")
            root_name = final_name(root_handle)
            file_name = final_name(msvcrt.get_osfhandle(fd))
            expected_root = os.path.normcase(os.path.normpath(str(root)))
            try:
                contained = os.path.commonpath((root_name, file_name)) == root_name
            except ValueError:
                contained = False
            if root_name != expected_root or not contained:
                raise OSError("workspace file escaped its root")
            return os.fdopen(fd, "rb")
        except BaseException:
            os.close(fd)
            raise
    finally:
        kernel.CloseHandle(root_handle)


def open_resolved_workspace_file(root: Path, candidate: Path) -> BinaryIO:
    """Open a resolved candidate and bind subsequent reads to that file handle."""
    root = root.resolve()
    try:
        relative = candidate.relative_to(root)
    except ValueError as exc:
        raise OSError("workspace file escaped its root") from exc
    if not relative.parts or ".." in relative.parts:
        raise OSError("workspace file is not a regular file")
    if os.name == "nt":
        return _open_windows(root, candidate)
    if os.open not in os.supports_dir_fd or not hasattr(os, "O_NOFOLLOW"):
        raise OSError("secure workspace file access is unavailable")
    return _open_posix(root, relative)
