"""Exercise Windows lock initialization and cleanup on every CI platform."""

import os
from types import SimpleNamespace

import pytest

from qwenpaw_data.host.core.session import _locking


@pytest.fixture
def windows_lock(monkeypatch):
    calls = []

    def locking(fd, mode, count):
        calls.append((fd, mode, count))

    api = SimpleNamespace(LK_LOCK=1, LK_UNLCK=2, locking=locking)
    monkeypatch.setattr(_locking, "fcntl", None)
    monkeypatch.setattr(_locking, "msvcrt", api)
    return api, calls


def assert_closed(fd):
    with pytest.raises(OSError):
        os.fstat(fd)


def test_empty_lock_file_needs_no_initialization_write(tmp_path, windows_lock):
    api, calls = windows_lock
    with _locking.file_lock(tmp_path / "session.json"):
        # No write is needed before acquiring the byte range. A competing
        # process may already own it, even when the file is still empty.
        assert (tmp_path / ".session.json.lock").stat().st_size == 0
        fd = calls[0][0]
        assert calls == [(fd, api.LK_LOCK, 1)]
    assert calls == [(fd, api.LK_LOCK, 1), (fd, api.LK_UNLCK, 1)]
    assert_closed(fd)


def test_failed_acquisition_preserves_error_and_closes_fd(tmp_path, windows_lock):
    api, calls = windows_lock
    error = PermissionError("another worker holds the lock")

    def fail_acquisition(fd, mode, count):
        calls.append((fd, mode, count))
        raise error

    api.locking = fail_acquisition
    with pytest.raises(PermissionError) as caught:
        with _locking.file_lock(tmp_path / "session.json"):
            pytest.fail("failed acquisition must not enter the critical section")
    assert caught.value is error
    assert len(calls) == 1
    fd, mode, count = calls[0]
    assert (mode, count) == (api.LK_LOCK, 1)
    assert_closed(fd)


def test_failed_release_still_closes_fd(tmp_path, windows_lock):
    api, calls = windows_lock

    def fail_release(fd, mode, count):
        calls.append((fd, mode, count))
        if mode == api.LK_UNLCK:
            raise PermissionError("release failed")

    api.locking = fail_release
    with pytest.raises(PermissionError, match="release failed"):
        with _locking.file_lock(tmp_path / "session.json"):
            pass
    assert len(calls) == 2
    assert_closed(calls[0][0])


def test_body_error_releases_lock_and_closes_fd(tmp_path, windows_lock):
    api, calls = windows_lock
    with pytest.raises(ValueError, match="write failed"):
        with _locking.file_lock(tmp_path / "session.json"):
            raise ValueError("write failed")
    fd = calls[0][0]
    assert calls == [(fd, api.LK_LOCK, 1), (fd, api.LK_UNLCK, 1)]
    assert_closed(fd)
