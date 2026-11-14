"""Non-overwriting workspace publication for the SQL import file hook.

An authenticated intent is durable BEFORE each hard link. The retained staging
inode proves ownership on retry; equal bytes in an unrelated file are never
adopted. No cleanup or rollback is performed here. All writers must stay stopped.
"""

import base64
from contextlib import contextmanager
from dataclasses import dataclass
import hashlib
import hmac
import os
from pathlib import Path
import re
import stat
from uuid import uuid4

from cryptography.fernet import Fernet, InvalidToken

from .binding import database_binding
from .bundle import RecoveryPlan, _validate_plan
from .legacy_json import MigrationError, _canonical, _portable_key
from .locking import MigrationLock
from .sql_import import plan_digest, _run_id
from . import staging


_TEMP = re.compile(r"\.publish-[0-9a-f]{32}\.tmp")
_RECORD = re.compile(r"publish-(dir|file)-([0-9]{8})\.enc")


@dataclass(frozen=True)
class PublicationReceipt:
    run_id: str
    digest: str
    files: int
    directories: int

    def report(self):
        return {
            "stage": "verified_workspace_publication",
            "run_id": self.run_id,
            "plan_digest": self.digest,
            "verified_files": self.files,
            "verified_directories": self.directories,
            "sql_commit_verified": False,
        }


def _cipher(key):
    if type(key) is not bytes or len(key) != 32:
        raise MigrationError("Target master key must contain exactly 32 raw bytes")
    derived = hmac.new(
        key, b"qwenpaw-migration-publication-v1", hashlib.sha256
    ).digest()
    return Fernet(base64.urlsafe_b64encode(derived))


def _read_optional(fd, name, cipher):
    try:
        return staging._read_record(fd, name, cipher)
    except FileNotFoundError:
        return None


def _once(fd, name, document, cipher):
    current = _read_optional(fd, name, cipher)
    if current is not None:
        if _canonical(current) != _canonical(document):
            raise MigrationError("Publication journal differs from the recorded target")
        return
    if sum(bool(_TEMP.fullmatch(n)) for n in os.listdir(fd)) >= 32:
        raise MigrationError(
            "Too many interrupted journal writes; retain and inspect them"
        )
    temporary = f".publish-{uuid4().hex}.tmp"
    # Private journal + held home lock exclude cooperating concurrent writers.
    # Interrupted temporary records are retained, never automatically removed.
    staging._write_record(fd, temporary, document, cipher)
    try:
        os.stat(name, dir_fd=fd, follow_symlinks=False)
    except FileNotFoundError:
        pass
    else:
        raise MigrationError("Publication journal destination unexpectedly appeared")
    os.rename(temporary, name, src_dir_fd=fd, dst_dir_fd=fd)
    os.fsync(fd)


def _directory_info(info):
    if (
        not stat.S_ISDIR(info.st_mode)
        or info.st_uid != os.geteuid()
        or stat.S_IMODE(info.st_mode) & 0o022
    ):
        raise MigrationError(
            "Workspace ancestors must be owned directories without untrusted write access"
        )
    return [info.st_dev, info.st_ino]


def _alias(fd, name):
    if any(
        n != name and _portable_key(n) == _portable_key(name) for n in os.listdir(fd)
    ):
        raise MigrationError("Workspace path has a portable filesystem alias")


@contextmanager
def _walk(lock, relative):
    """Read-only, descriptor-relative traversal; never follow directory links."""
    fd = os.dup(lock.home_fd)
    try:
        for name in Path(relative).parts:
            _alias(fd, name)
            child = os.open(
                name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd
            )
            os.close(fd)
            fd = child
            _directory_info(os.fstat(fd))
        yield fd
    finally:
        os.close(fd)


def _base(recovery):
    return {"version": 1, "run_id": recovery.run_id, "plan_digest": recovery.digest}


def _dir_record(recovery, relative, fd):
    return {
        **_base(recovery),
        "directory": relative,
        "identity": _directory_info(os.fstat(fd)),
        "ownership": "observed_not_removable",
    }


def _intent(recovery, item, parent):
    placement = recovery.plan.files[item.index]
    return {
        **_base(recovery),
        "index": item.index,
        "destination": placement.destination,
        "device": item.device,
        "inode": item.inode,
        "size": item.size,
        "sha256": item.sha256,
        "parent_identity": _directory_info(os.fstat(parent)),
    }


def _file_state(run, recovery, item, lock, cipher, *, required):
    relative = Path(recovery.plan.files[item.index].destination)
    name = f"publish-file-{item.index:08d}.enc"
    record = _read_optional(run, name, cipher)
    try:
        with _walk(lock, relative.parent) as parent:
            _alias(parent, relative.name)
            expected = _intent(recovery, item, parent)
            if record is not None and _canonical(record) != _canonical(expected):
                raise MigrationError(
                    "File publication intent or parent directory changed"
                )
            try:
                target = os.stat(relative.name, dir_fd=parent, follow_symlinks=False)
            except FileNotFoundError:
                target = None
    except FileNotFoundError:
        if record is not None:
            raise MigrationError(
                "Previously recorded publication parent is missing"
            ) from None
        target = None
    current = item.path.lstat()
    staging._private(current, links=2 if target is not None else 1)
    if (current.st_dev, current.st_ino) != (item.device, item.inode):
        raise MigrationError("Staging inode changed before publication")
    if target is not None:
        if (
            record is None
            or not stat.S_ISREG(target.st_mode)
            or (target.st_dev, target.st_ino) != (item.device, item.inode)
            or target.st_nlink != 2
            or stat.S_IMODE(target.st_mode) != 0o600
        ):
            raise MigrationError(
                "Workspace file already exists without matching owned publication evidence"
            )
        return True
    if required:
        raise MigrationError("Recorded workspace publication is incomplete")
    return False


@contextmanager
def _prepared(recovery, lock, database_url, key):
    lock.check()
    _run_id(recovery.run_id)
    _validate_plan(recovery.plan)
    if (
        lock.home != recovery.plan.target_home
        or lock.identity != recovery.home_identity
        or plan_digest(recovery.plan) != recovery.digest
        or database_binding(database_url, key) != recovery.database_binding
    ):
        raise MigrationError(
            "Publication selection differs from the bound recovery plan"
        )
    if any(
        _portable_key(p.split("/")[0]) in {staging._ROOT, ".legacy-migration.lock"}
        for p in recovery.plan.directories
    ):
        raise MigrationError("Workspace scope conflicts with reserved migration paths")
    _alias(lock.home_fd, staging._ROOT)
    with staging._directory(lock.home_fd, staging._ROOT) as (root, _):
        with staging._directory(root, recovery.run_id) as (run, _):
            stage_cipher = staging._cipher(key)
            if _canonical(
                staging._read_record(run, "binding.enc", stage_cipher)
            ) != _canonical(staging._binding(recovery, run)):
                raise MigrationError("Staging root identity changed before publication")
            files = {}
            names = os.listdir(run)
            if (
                len(names)
                > 2 * len(recovery.plan.files) + len(recovery.plan.directories) + 133
            ):
                raise MigrationError("Publication journal contains too many entries")
            path = lock.home / staging._ROOT / recovery.run_id
            for name in names:
                if name == "binding.enc" or _TEMP.fullmatch(name):
                    continue
                record = _RECORD.fullmatch(name)
                if record:
                    limit = len(
                        recovery.plan.directories
                        if record[1] == "dir"
                        else recovery.plan.files
                    )
                    if int(record[2]) >= limit:
                        raise MigrationError(
                            "Publication journal has an out-of-range record"
                        )
                    continue
                match = staging._ATTEMPT.fullmatch(name)
                if match is None or int(match[2]) >= len(recovery.plan.files):
                    raise MigrationError("Publication journal has an unexpected entry")
                if match[1] == "pending":
                    with staging._directory(run, name):
                        pass
                    continue
                index = int(match[2])
                if index in files:
                    raise MigrationError("Duplicate completed staging attempts")
                with staging._directory(run, name) as (attempt, _):
                    links = os.stat(
                        "data", dir_fd=attempt, follow_symlinks=False
                    ).st_nlink
                if links not in {1, 2}:
                    raise MigrationError("Staging file has unexpected additional links")
                files[index] = staging._completed(
                    run, name, index, recovery, stage_cipher, path, links=links
                )
            if len(files) != len(recovery.plan.files):
                raise MigrationError(
                    "All file staging must complete before publication"
                )
            yield run, tuple(files[i] for i in range(len(files))), _cipher(key)
            lock.check()


def _directories(run, recovery, lock, cipher, *, create):
    # Sorted parent-first, but journal IDs always refer to the original plan.
    for index, relative in sorted(
        enumerate(recovery.plan.directories),
        key=lambda pair: (pair[1].count("/"), pair[1]),
    ):
        name = f"publish-dir-{index:08d}.enc"
        record = _read_optional(run, name, cipher)
        path = Path(relative)
        with _walk(lock, path.parent) as parent:
            _alias(parent, path.name)
            if create and record is None:
                try:
                    os.mkdir(path.name, mode=0o700, dir_fd=parent)
                    os.fsync(parent)
                except FileExistsError:
                    pass
            with _walk(lock, relative) as directory:
                expected = _dir_record(recovery, relative, directory)
                if record is not None:
                    if _canonical(record) != _canonical(expected):
                        raise MigrationError(
                            "Observed workspace directory was replaced"
                        )
                elif create:
                    _once(run, name, expected, cipher)
                else:
                    raise MigrationError(
                        "Workspace directory has no publication record"
                    )


def publish_files(
    recovery: RecoveryPlan, *, lock: MigrationLock, database_url: str, key: bytes
) -> PublicationReceipt:
    """Use ONLY in commit_sql_rows' hook with its target_anchor check enabled.

    No independent SQL connection is opened inside the active transaction. The
    caller must verify the SQL outcome before cleanup (none is done here).
    Hard-link publication requires staging and destination on the same filesystem.
    Retained stage links must remain until coordinated recovery/cleanup completes.
    """
    try:
        with _prepared(recovery, lock, database_url, key) as (run, files, cipher):
            # Conflicts anywhere stop before any file is published.
            for item in files:
                _file_state(run, recovery, item, lock, cipher, required=False)
            _directories(run, recovery, lock, cipher, create=True)
            for item in files:
                relative = Path(recovery.plan.files[item.index].destination)
                with _walk(lock, relative.parent) as parent:
                    if os.fstat(parent).st_dev != item.device:
                        raise MigrationError(
                            "Workspace and staging must share a filesystem for atomic no-overwrite publication"
                        )
            for item in files:
                lock.check()
                if _file_state(run, recovery, item, lock, cipher, required=False):
                    continue
                relative = Path(recovery.plan.files[item.index].destination)
                with _walk(lock, relative.parent) as parent:
                    _once(
                        run,
                        f"publish-file-{item.index:08d}.enc",
                        _intent(recovery, item, parent),
                        cipher,
                    )
                    with staging._directory(run, item.path.parent.name) as (attempt, _):
                        # link is atomic and refuses ANY existing destination.
                        os.link(
                            "data",
                            relative.name,
                            src_dir_fd=attempt,
                            dst_dir_fd=parent,
                            follow_symlinks=False,
                        )
                    os.fsync(parent)
            _directories(run, recovery, lock, cipher, create=False)
            for item in files:
                staging._completed(
                    run,
                    item.path.parent.name,
                    item.index,
                    recovery,
                    staging._cipher(key),
                    item.path.parent.parent,
                    links=2,
                )
                _file_state(run, recovery, item, lock, cipher, required=True)
            return PublicationReceipt(
                recovery.run_id,
                recovery.digest,
                len(files),
                len(recovery.plan.directories),
            )
    except (OSError, ValueError, TypeError, InvalidToken) as exc:
        if isinstance(exc, MigrationError):
            raise
        raise MigrationError(
            "Workspace publication failed; retain staging and journal, then inspect the bound SQL receipt"
        ) from None


def verify_publication(
    recovery: RecoveryPlan, *, lock: MigrationLock, database_url: str, key: bytes
) -> PublicationReceipt:
    """Read-only file verification; caller separately verifies the bound SQL receipt."""
    try:
        with _prepared(recovery, lock, database_url, key) as (run, files, cipher):
            _directories(run, recovery, lock, cipher, create=False)
            for item in files:
                _file_state(run, recovery, item, lock, cipher, required=True)
            return PublicationReceipt(
                recovery.run_id,
                recovery.digest,
                len(files),
                len(recovery.plan.directories),
            )
    except (OSError, ValueError, TypeError, InvalidToken) as exc:
        if isinstance(exc, MigrationError):
            raise
        raise MigrationError(
            "Cannot verify workspace publication; no repair or cleanup was attempted"
        ) from None
