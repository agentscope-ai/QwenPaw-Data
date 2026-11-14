"""Private resumable backup copies, not final workspace publication.

Completed attempts have authenticated, inode-bound receipts. Interrupted attempts
are retained, never overwritten/deleted, and bounded before another copy starts.
Only the future SQL/file coordinator may publish or clean up these copies.
"""

from contextlib import contextmanager
from dataclasses import dataclass, field
import base64
import hashlib
import hmac
import os
from pathlib import Path
import re
import stat
from uuid import uuid4

from cryptography.fernet import Fernet, InvalidToken

from .binding import assert_target_anchor, database_binding
from .bundle import RecoveryPlan, _validate_plan
from .legacy_json import (
    MigrationError,
    _canonical,
    _chunks,
    _json,
    _portable_key,
    _read_file,
    _signature,
    verify_backup,
)
from .locking import MigrationLock
from .sql_import import plan_digest, read_sql_receipt
from .target import _read_only_target, check_target_connection


_ROOT = ".legacy-migrations"
_ATTEMPT = re.compile(r"(pending|ready)-([0-9]{8})-([0-9a-f]{32})")
_MAX_RECORD = 16384


@dataclass(frozen=True)
class StagedFile:
    index: int
    path: Path = field(repr=False)
    device: int
    inode: int
    size: int
    sha256: str


@dataclass(frozen=True)
class StagingReceipt:
    run_id: str
    digest: str
    files: tuple[StagedFile, ...] = field(repr=False)
    incomplete_attempts: int

    def report(self):
        return {
            "stage": "private_file_staging",
            "run_id": self.run_id,
            "plan_digest": self.digest,
            "verified_files": len(self.files),
            "retained_incomplete_attempts": self.incomplete_attempts,
            "workspace_files_published": False,
            "sql_written": False,
            "import_ready": False,
        }


def _cipher(key):
    if type(key) is not bytes or len(key) != 32:
        raise MigrationError("Target master key must contain exactly 32 raw bytes")
    derived = hmac.new(
        key, b"qwenpaw-migration-file-staging-v1", hashlib.sha256
    ).digest()
    return Fernet(base64.urlsafe_b64encode(derived))


def _private(info, *, directory=False):
    if (
        not (stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode))
        or info.st_uid != os.geteuid()
        or stat.S_IMODE(info.st_mode) != (0o700 if directory else 0o600)
        or (not directory and info.st_nlink != 1)
    ):
        raise MigrationError(
            "Staging entry must be private, owned and without file aliases"
        )


@contextmanager
def _directory(parent, name, *, create=False):
    created = False
    if create:
        try:
            os.mkdir(name, mode=0o700, dir_fd=parent)
            created = True
            os.fsync(parent)
        except FileExistsError:
            pass
    fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
    try:
        info = os.fstat(fd)
        _private(info, directory=True)
        yield fd, created
        named = os.stat(name, dir_fd=parent, follow_symlinks=False)
        _private(named, directory=True)
        if (info.st_dev, info.st_ino) != (named.st_dev, named.st_ino):
            raise MigrationError("Staging directory changed during access")
    finally:
        os.close(fd)


def _write_record(fd, name, document, cipher):
    data = cipher.encrypt(_canonical(document))
    if len(data) > _MAX_RECORD:
        raise MigrationError("Staging receipt exceeds its byte limit")
    output = os.open(
        name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=fd
    )
    with os.fdopen(output, "wb") as stream:
        os.fchmod(stream.fileno(), 0o600)
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())
    os.fsync(fd)


def _read_record(fd, name, cipher):
    opened = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
    with os.fdopen(opened, "rb") as stream:
        info = os.fstat(stream.fileno())
        _private(info)
        if info.st_size > _MAX_RECORD:
            raise MigrationError("Staging receipt exceeds its byte limit")
        raw = stream.read(_MAX_RECORD + 1)
        if _signature(os.fstat(stream.fileno())) != _signature(info) or _signature(
            os.stat(name, dir_fd=fd, follow_symlinks=False)
        ) != _signature(info):
            raise MigrationError("Staging receipt changed during read")
    return _json(cipher.decrypt(raw), "private staging receipt")


def _binding(recovery, fd):
    info = os.fstat(fd)
    return {
        "version": 1,
        "run_id": recovery.run_id,
        "plan_digest": recovery.digest,
        "home_identity": list(recovery.home_identity),
        "stage_identity": [info.st_dev, info.st_ino],
        "target_anchor": recovery.target_anchor,
        "database_binding": recovery.database_binding,
    }


def _verify_data(fd, placement):
    opened = os.open("data", os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
    with os.fdopen(opened, "rb") as stream:
        info = os.fstat(stream.fileno())
        _private(info)
        if info.st_size != placement.size:
            raise MigrationError("Staged file size differs from its source inventory")
        digest = hashlib.sha256()
        for chunk in _chunks(stream, placement.size):
            digest.update(chunk)
        if digest.hexdigest() != placement.sha256:
            raise MigrationError("Staged file hash differs from its source inventory")
        if _signature(os.fstat(stream.fileno())) != _signature(info) or _signature(
            os.stat("data", dir_fd=fd, follow_symlinks=False)
        ) != _signature(info):
            raise MigrationError("Staged file changed during verification")
    return {
        "device": info.st_dev,
        "inode": info.st_ino,
        "size": placement.size,
        "sha256": placement.sha256,
    }


def _copy_data(fd, backup, placement):
    source_info = (backup / placement.source).lstat()
    with _read_file(backup, placement.source, _signature(source_info)) as source:
        output = os.open(
            "data",
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            0o600,
            dir_fd=fd,
        )
        with os.fdopen(output, "wb") as destination:
            os.fchmod(destination.fileno(), 0o600)
            digest = hashlib.sha256()
            for chunk in _chunks(source, placement.size):
                digest.update(chunk)
                destination.write(chunk)
            if digest.hexdigest() != placement.sha256:
                raise MigrationError("Backup file changed while copying to staging")
            destination.flush()
            os.fsync(destination.fileno())
    os.fsync(fd)


def _completed(fd, name, index, recovery, cipher, root_path):
    with _directory(fd, name) as (attempt, _):
        if set(os.listdir(attempt)) != {"data", "receipt.enc"}:
            raise MigrationError("Completed staging attempt has unexpected entries")
        actual = _read_record(attempt, "receipt.enc", cipher)
        identity = _verify_data(attempt, recovery.plan.files[index])
        expected = {
            "version": 1,
            "run_id": recovery.run_id,
            "plan_digest": recovery.digest,
            "index": index,
            "attempt": name.removeprefix("ready-"),
            **identity,
        }
        if _canonical(actual) != _canonical(expected):
            raise MigrationError(
                "Staging receipt differs from the original plan or file"
            )
    return StagedFile(index, root_path / name / "data", **identity)


def stage_files(
    recovery: RecoveryPlan,
    *,
    lock: MigrationLock,
    database_url: str,
    key: bytes,
    max_incomplete_attempts: int = 8,
) -> StagingReceipt:
    """Copy from an independent backup while holding the target-home lock.

    No native workspace paths or business SQL rows are written. A crash during
    first run-directory initialization fails closed for operator inspection.
    Later incomplete copies are retained, and a new bounded attempt may resume.
    The source need not be online; the complete backup must still verify.
    """
    try:
        if (
            type(max_incomplete_attempts) is not int
            or not 1 <= max_incomplete_attempts <= 100
        ):
            raise MigrationError(
                "Incomplete staging attempt limit must be between 1 and 100"
            )
        lock.check()
        _validate_plan(recovery.plan)
        reserved = {_ROOT, ".legacy-migration.lock"}
        if any(
            _portable_key(relative.split("/")[0]) in reserved
            for relative in recovery.plan.directories
        ):
            raise MigrationError(
                "Native workspace scope conflicts with reserved migration paths"
            )
        if any(
            name != _ROOT and _portable_key(name) == _ROOT
            for name in os.listdir(lock.home_fd)
        ):
            raise MigrationError("Staging root has a portable filesystem alias")
        if (
            lock.home != recovery.plan.target_home
            or lock.identity != recovery.home_identity
            or plan_digest(recovery.plan) != recovery.digest
            or database_binding(database_url, key) != recovery.database_binding
        ):
            raise MigrationError(
                "Staging selection differs from the recorded recovery plan"
            )
        cipher = _cipher(key)
        # Never treat an inaccessible, replaced, or already committed database as
        # permission to create fresh file copies for an uncommitted import.
        receipt = read_sql_receipt(
            recovery.plan,
            database_url=database_url,
            run_id=recovery.run_id,
            target_stopped=True,
            target_anchor=recovery.target_anchor,
        )
        if receipt is not None:
            raise MigrationError(
                "SQL is already committed; use coordinated recovery, not fresh staging"
            )
        with _read_only_target(database_url) as conn:
            assert_target_anchor(conn, recovery.target_anchor)
            check_target_connection(recovery.plan, conn)
        verify_backup(recovery.backup, recovery.plan.source)
        lock.check()
        with _directory(lock.home_fd, _ROOT, create=True) as (root, _):
            with _directory(root, recovery.run_id, create=True) as (run, created):
                binding = _binding(recovery, run)
                if created:
                    _write_record(run, "binding.enc", binding, cipher)
                if _canonical(_read_record(run, "binding.enc", cipher)) != _canonical(
                    binding
                ):
                    raise MigrationError(
                        "Staging root belongs to a different plan or directory"
                    )
                names = os.listdir(run)
                if len(names) > len(recovery.plan.files) + max_incomplete_attempts + 1:
                    raise MigrationError(
                        "Staging contains too many retained attempts; inspect before retrying"
                    )
                root_path = lock.home / _ROOT / recovery.run_id
                files, incomplete = {}, 0
                for name in sorted(names):
                    if name == "binding.enc":
                        continue
                    match = _ATTEMPT.fullmatch(name)
                    if match is None or int(match[2]) >= len(recovery.plan.files):
                        raise MigrationError("Staging contains an unrecognized entry")
                    index = int(match[2])
                    if match[1] == "pending":
                        # Do not follow, delete, truncate or reuse interrupted copies.
                        with _directory(run, name):
                            pass
                        incomplete += 1
                    else:
                        if index in files:
                            raise MigrationError(
                                "Staging contains duplicate completed file attempts"
                            )
                        files[index] = _completed(
                            run, name, index, recovery, cipher, root_path
                        )
                if incomplete >= max_incomplete_attempts and len(files) != len(
                    recovery.plan.files
                ):
                    raise MigrationError(
                        "Incomplete staging attempt limit reached; preserve and inspect copies"
                    )
                for index, placement in enumerate(recovery.plan.files):
                    if index in files:
                        continue
                    lock.check()
                    suffix = f"{index:08d}-{uuid4().hex}"
                    pending, ready = f"pending-{suffix}", f"ready-{suffix}"
                    # Exclusive creation: UUID collisions never adopt another attempt.
                    os.mkdir(pending, mode=0o700, dir_fd=run)
                    os.fsync(run)
                    with _directory(run, pending) as (attempt, _):
                        _copy_data(attempt, recovery.backup, placement)
                        identity = _verify_data(attempt, placement)
                        _write_record(
                            attempt,
                            "receipt.enc",
                            {
                                "version": 1,
                                "run_id": recovery.run_id,
                                "plan_digest": recovery.digest,
                                "index": index,
                                "attempt": suffix,
                                **identity,
                            },
                            cipher,
                        )
                    # Only our locked, private, unpredictable attempt is renamed.
                    # A destination of any kind is a conflict, never reused.
                    try:
                        os.stat(ready, dir_fd=run, follow_symlinks=False)
                    except FileNotFoundError:
                        pass
                    else:
                        raise MigrationError(
                            "Completed staging destination already exists"
                        )
                    os.rename(pending, ready, src_dir_fd=run, dst_dir_fd=run)
                    os.fsync(run)
                    files[index] = _completed(
                        run, ready, index, recovery, cipher, root_path
                    )
                lock.check()
                return StagingReceipt(
                    recovery.run_id,
                    recovery.digest,
                    tuple(files[i] for i in range(len(files))),
                    incomplete,
                )
    except (OSError, ValueError, TypeError, InvalidToken) as exc:
        if isinstance(exc, MigrationError):
            raise
        raise MigrationError(
            "Private file staging failed; retain all copies and inspect before retrying"
        ) from None
