"""Cross-process file locking and atomic-write helpers for JSON stores."""

from __future__ import annotations

import errno
import json
import os
import tempfile
import threading
from contextlib import contextmanager
from pathlib import Path

try:
    import fcntl
except ImportError:  # pragma: no cover - exercised by Windows CI
    fcntl = None  # type: ignore[assignment]

try:
    import msvcrt
except ImportError:  # pragma: no cover - exercised by POSIX CI
    msvcrt = None  # type: ignore[assignment]


_PATH_LOCKS_GUARD = threading.Lock()
_PATH_LOCKS: dict[Path, threading.RLock] = {}


def _process_lock(path: Path) -> threading.RLock:
    """Return the process-wide lock shared by all store instances."""
    with _PATH_LOCKS_GUARD:
        return _PATH_LOCKS.setdefault(path, threading.RLock())


@contextmanager
def file_lock(path: Path):
    """Serialize one file across threads, instances and worker processes."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with _process_lock(path):
        lock_path = path.with_name(f".{path.name}.lock")
        fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            if hasattr(os, "fchmod"):
                try:
                    os.fchmod(fd, 0o600)
                except OSError:
                    pass
            if fcntl is not None:
                fcntl.flock(fd, fcntl.LOCK_EX)
            elif msvcrt is not None:
                if os.fstat(fd).st_size == 0:
                    os.write(fd, b"\0")
                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_LOCK, 1)
            else:  # pragma: no cover - all supported platforms provide one
                raise RuntimeError("No process file-lock implementation available")
            yield
        finally:
            if fcntl is not None:
                fcntl.flock(fd, fcntl.LOCK_UN)
            elif msvcrt is not None:
                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
            os.close(fd)


def write_atomic(path: Path, state: dict) -> None:
    """Write via a sibling temp file + os.replace so readers never see
    a partially written JSON document."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(state, handle, ensure_ascii=False, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, path)
        # Persist the directory entry as well as the file contents. Some
        # filesystems do not support directory fsync; atomic replace still
        # protects readers in that case.
        if os.name != "nt":
            dir_fd = os.open(path.parent, os.O_RDONLY)
            try:
                try:
                    os.fsync(dir_fd)
                except OSError as exc:
                    if exc.errno not in {errno.EINVAL, errno.ENOTSUP}:
                        raise
            finally:
                os.close(dir_fd)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except FileNotFoundError:
            pass
        raise
