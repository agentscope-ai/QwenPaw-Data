"""Private authenticated recovery plans; not a file publisher or CLI importer.

The original converted rows (including ciphertext and timestamps) survive process
restart. Loading does not reconnect, replan, create keys, or assume SQL committed.
The coordinator must pass target_anchor to every SQL receipt/import operation.
"""

from __future__ import annotations

import base64
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime
import hashlib
import hmac
import os
from pathlib import Path, PurePosixPath
import re
import stat
from uuid import uuid4

from cryptography.fernet import Fernet, InvalidToken
from pydantic import TypeAdapter

from qwenpaw_data.host.core.db.tables import Base
from qwenpaw_data.host.core.db.types import TZDateTime

from .binding import assert_target_anchor, database_binding
from .legacy_json import (
    MigrationError,
    _canonical,
    _component,
    _json,
    _kind,
    _portable_key,
    _signature,
    identity_mapping,
    scan_source,
    verify_backup,
)
from .plan import MigrationPlan
from .sql_import import _run_id, plan_digest
from .target import _read_only_target


_ADAPTER = TypeAdapter(MigrationPlan)
_MAGIC = b"QWENPAW-MIGRATION-PLAN-V1\n"
_FORMAT = "qwenpaw-data-private-migration-plan"
_FIELDS = {
    "format",
    "version",
    "run_id",
    "plan_digest",
    "backup",
    "target_anchor",
    "database_binding",
    "home_identity",
    "plan",
}
_MAX_BYTES = 64 * 1024**2


def _cipher(key: bytes) -> Fernet:
    if type(key) is not bytes or len(key) != 32:
        raise MigrationError("Target master key must contain exactly 32 raw bytes")
    derived = hmac.new(
        key, b"qwenpaw-migration-private-plan-v1", hashlib.sha256
    ).digest()
    return Fernet(base64.urlsafe_b64encode(derived))


def _limit(value: int) -> int:
    if type(value) is not int or value <= 0:
        raise MigrationError("Recovery-plan byte limit must be a positive integer")
    return value * 4 // 3 + 4096


def _absolute(path: Path) -> None:
    if not path.is_absolute() or ".." in path.parts or path.parent == path:
        raise MigrationError("Recovery plan requires explicit non-root absolute paths")


def _relative(value: str) -> tuple[str, ...]:
    path = PurePosixPath(value)
    if not value or not path.parts or path.is_absolute() or path.as_posix() != value:
        raise MigrationError("Recovery plan contains an unsafe relative path")
    return tuple(_component(part) for part in path.parts)


def _validate_plan(plan: MigrationPlan) -> None:
    plan_digest(plan)  # Complete plan, supported tables, canonical finite values.
    _absolute(plan.source.home)
    _absolute(plan.target_home)
    if plan.target_home.is_relative_to(
        plan.source.home
    ) or plan.source.home.is_relative_to(plan.target_home):
        raise MigrationError("Recovery source and target must be disjoint")
    identity_mapping(
        {
            "version": 1,
            "users": {
                str(n): vars(identity) for n, identity in enumerate(plan.identities)
            },
        }
    )
    scope = {(i.user_id, i.tenant_id, i.workspace_id) for i in plan.identities}
    users = {i.user_id for i in plan.identities}
    primary_keys, sessions = set(), {}
    for row in plan.rows:
        table = Base.metadata.tables[row.table]
        if (
            set(row.values) - set(table.columns.keys())
            or row.values.get("user_id") not in users
        ):
            raise MigrationError("Recovery row columns or identity are invalid")
        if (
            "tenant_id" in row.values
            and tuple(
                row.values.get(k) for k in ("user_id", "tenant_id", "workspace_id")
            )
            not in scope
        ):
            raise MigrationError(
                "Recovery row identity differs from its recorded scope"
            )
        key = (
            row.table,
            *(row.values[column.name] for column in table.primary_key.columns),
        )
        if key in primary_keys:
            raise MigrationError("Recovery plan contains duplicate primary keys")
        primary_keys.add(key)
        if row.table == "sessions":
            sessions[row.values["id"]] = row.values
    names = set()
    for relative in [*plan.source.directories, *(f.path for f in plan.source.files)]:
        if _relative(relative)[0] != "host" or _portable_key(relative) in names:
            raise MigrationError(
                "Recovery source inventory contains invalid or aliased paths"
            )
        names.add(_portable_key(relative))
    if (
        len(plan.source.files) > plan.source.limits.files
        or len(plan.source.directories) > plan.source.limits.files
        or sum(f.size for f in plan.source.files) > plan.source.limits.bytes
    ):
        raise MigrationError("Recovery source inventory exceeds its recorded limits")
    for file in plan.source.files:
        if (
            file.size < 0
            or not re.fullmatch(r"[0-9a-f]{64}", file.sha256)
            or not 0 <= file.source_mode <= 0o7777
            or len(file.signature) != 5
            or file.signature[2] != file.size
            or file.kind != _kind(file.path)
        ):
            raise MigrationError("Recovery source inventory metadata is invalid")

    def prefix(session):
        return "/".join(
            [
                *(
                    _component(session[k])
                    for k in ("tenant_id", "workspace_id", "user_id", "agent_id")
                ),
                "workspace",
            ]
        )

    def destination(relative):
        parts = _relative(relative)
        if (
            len(parts) < 4
            or parts[:2] != ("host", "workspace")
            or parts[2] not in {"uploads", "artifacts"}
            or parts[3] not in sessions
        ):
            raise MigrationError("Recovery file has no converted owning session")
        return prefix(sessions[parts[3]]) + "/" + "/".join(parts[2:])

    directories = set()

    def include(relative, *, file=False):
        parts = _relative(relative)
        for index in range(1, len(parts) + (not file)):
            directories.add("/".join(parts[:index]))

    for session in sessions.values():
        include(prefix(session))
    for relative in plan.source.directories:
        parts = PurePosixPath(relative).parts
        if (
            len(parts) >= 4
            and parts[:2] == ("host", "workspace")
            and parts[2] in {"uploads", "artifacts"}
        ):
            include(destination(relative))
    expected = {
        f.path: f for f in plan.source.files if f.kind in {"uploads", "artifacts"}
    }
    destinations = set()
    for file in plan.files:
        original = expected.pop(file.source, None)
        if (
            original is None
            or (file.size, file.sha256, file.source_mode)
            != (original.size, original.sha256, original.source_mode)
            or file.destination != destination(file.source)
            or file.destination in destinations
        ):
            raise MigrationError(
                "Recovery file placement differs from its scoped source inventory"
            )
        destinations.add(file.destination)
        include(file.destination, file=True)
    if (
        expected
        or len(plan.directories) != len(directories)
        or set(plan.directories) != directories
    ):
        raise MigrationError(
            "Recovery files/directories do not match the complete scoped inventory"
        )
    all_paths = directories | destinations
    if directories & destinations or len(
        {_portable_key(path) for path in all_paths}
    ) != len(all_paths):
        raise MigrationError("Recovery target paths collide")


def _restore_plan(document) -> MigrationPlan:
    # Strict JSON preserves dataclass types; compare the round trip to reject
    # ignored unknown fields at EVERY typed level, not merely the outer envelope.
    plan = _ADAPTER.validate_json(_canonical(document), strict=True)
    if _canonical(_ADAPTER.dump_python(plan, mode="json")) != _canonical(document):
        raise MigrationError(
            "Recovery plan schema contains unsupported fields or representations"
        )
    for row in plan.rows:
        table = Base.metadata.tables.get(row.table)
        if table is None:
            raise MigrationError("Recovery plan contains an unsupported table")
        for column in table.columns:
            if (
                isinstance(column.type, TZDateTime)
                and row.values.get(column.name) is not None
            ):
                row.values[column.name] = datetime.fromisoformat(
                    row.values[column.name]
                )
    _validate_plan(plan)
    return plan


def _home_identity(home: Path) -> tuple[int, int]:
    _absolute(home)
    if home.is_symlink() or home.resolve() != home or not home.is_dir():
        raise MigrationError(
            "Recovery target home is absent, redirected or not a directory"
        )
    info = home.stat()
    if os.name == "posix" and (
        info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) & 0o022
    ):
        raise MigrationError(
            "Recovery target home must be owned and not group/world writable"
        )
    return info.st_dev, info.st_ino


@dataclass(frozen=True)
class RecoveryPlan:
    run_id: str
    plan: MigrationPlan = field(repr=False)
    backup: Path = field(repr=False)
    target_anchor: str = field(repr=False)
    database_binding: str = field(repr=False)
    home_identity: tuple[int, int] = field(repr=False)
    digest: str


def _decode(document, *, database_url, target_home, key) -> RecoveryPlan:
    if (
        not isinstance(document, dict)
        or set(document) != _FIELDS
        or document["format"] != _FORMAT
        or type(document["version"]) is not int
        or document["version"] != 1
    ):
        raise MigrationError("Unsupported private recovery-plan format")
    _run_id(document["run_id"])
    if (
        not isinstance(document["home_identity"], list)
        or len(document["home_identity"]) != 2
        or any(
            type(value) is not int or value < 0 for value in document["home_identity"]
        )
    ):
        raise MigrationError("Recovery plan has an invalid target home identity")
    if not isinstance(document["target_anchor"], str) or not re.fullmatch(
        r"[0-9a-f]{32}", document["target_anchor"]
    ):
        raise MigrationError("Recovery plan has an invalid database token")
    plan = _restore_plan(document["plan"])
    fingerprint = plan_digest(plan)
    if document["plan_digest"] != fingerprint:
        raise MigrationError(
            "Recovery plan digest does not match its converted records"
        )
    if (
        plan.target_home != Path(target_home).resolve()
        or list(_home_identity(plan.target_home)) != document["home_identity"]
    ):
        raise MigrationError("Recovery target home differs from the original directory")
    binding = database_binding(database_url, key)
    if not isinstance(document["database_binding"], str) or not hmac.compare_digest(
        binding, document["database_binding"]
    ):
        raise MigrationError(
            "Recovery database selection differs from the recorded target"
        )
    backup = Path(document["backup"])
    _absolute(backup)
    for protected in (plan.source.home, plan.target_home):
        if backup.is_relative_to(protected) or protected.is_relative_to(backup):
            raise MigrationError("Recovery source, backup and target must be disjoint")
    return RecoveryPlan(
        document["run_id"],
        plan,
        backup,
        document["target_anchor"],
        binding,
        tuple(document["home_identity"]),
        fingerprint,
    )


@contextmanager
def _parent(path: Path):
    if os.name != "posix":
        raise MigrationError(
            "Private recovery-plan publication requires POSIX permissions; native Windows acceptance is pending"
        )
    selected = Path(path)
    _absolute(selected)
    name = _component(selected.name)
    parent = selected.parent.resolve(strict=True)
    fd = os.open(parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        info = os.fstat(fd)
        if info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) & 0o077:
            raise MigrationError(
                "Recovery-plan parent must be private and owned by the current user"
            )
        yield fd, name, parent / name
        current = parent.stat()
        if (current.st_dev, current.st_ino) != (info.st_dev, info.st_ino):
            raise MigrationError("Recovery-plan directory changed during access")
    finally:
        os.close(fd)


def _read(fd, name, key, max_bytes):
    bound = _limit(max_bytes)
    file = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
    try:
        info = os.fstat(file)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_nlink != 1
            or info.st_uid != os.geteuid()
            or stat.S_IMODE(info.st_mode) & 0o077
            or info.st_size > bound
        ):
            raise MigrationError(
                "Recovery-plan file must be private, owned, regular and within the byte limit"
            )
        with os.fdopen(os.dup(file), "rb") as stream:
            data = stream.read(bound + 1)
        if _signature(os.fstat(file)) != _signature(info) or _signature(
            os.stat(name, dir_fd=fd, follow_symlinks=False)
        ) != _signature(info):
            raise MigrationError("Recovery-plan file changed during read")
    finally:
        os.close(file)
    if not data.startswith(_MAGIC) or len(data) > bound:
        raise MigrationError("Invalid or oversized private recovery-plan file")
    plaintext = _cipher(key).decrypt(data[len(_MAGIC) :])
    if len(plaintext) > max_bytes:
        raise MigrationError("Decrypted recovery plan exceeds the byte limit")
    return _json(plaintext, "private recovery plan")


def load_recovery_plan(
    path: Path,
    *,
    database_url: str,
    target_home: Path,
    key: bytes,
    max_bytes: int = _MAX_BYTES,
) -> RecoveryPlan:
    """No DB connection or source/backup read; SQL must still check target_anchor."""
    try:
        with _parent(path) as (fd, name, _):
            return _decode(
                _read(fd, name, key, max_bytes),
                database_url=database_url,
                target_home=target_home,
                key=key,
            )
    except (OSError, ValueError, TypeError, KeyError, InvalidToken) as exc:
        if isinstance(exc, MigrationError):
            raise
        raise MigrationError(
            "Cannot authenticate, decode or safely load the private recovery plan"
        ) from None


def save_recovery_plan(
    path: Path,
    plan: MigrationPlan,
    *,
    backup: Path,
    database_url: str,
    run_id: str,
    target_anchor: str,
    key: bytes,
    source_stopped: bool = False,
    target_stopped: bool = False,
    max_bytes: int = _MAX_BYTES,
) -> str:
    """Persist the EXACT plan without replacement; explicit metadata/file write.

    The target database token must already exist; this does not create it. The
    verified backup remains independent. No source files or business rows change.
    """
    if source_stopped is not True or target_stopped is not True:
        raise MigrationError(
            "Saving a recovery plan requires stopped source and target writers"
        )
    if os.name != "posix":
        raise MigrationError(
            "Private recovery-plan publication requires POSIX permissions; native Windows acceptance is pending"
        )
    temporary = None
    try:
        _limit(max_bytes)
        cipher = _cipher(key)
        _validate_plan(plan)
        if (
            scan_source(plan.source.home, limits=plan.source.limits).digest
            != plan.source.digest
        ):
            raise MigrationError("Source changed before recovery-plan publication")
        backup = Path(backup).resolve(strict=True)
        verify_backup(backup, plan.source)
        document = {
            "format": _FORMAT,
            "version": 1,
            "run_id": run_id,
            "plan_digest": plan_digest(plan),
            "backup": str(backup),
            "target_anchor": target_anchor,
            "database_binding": database_binding(database_url, key),
            "home_identity": list(_home_identity(plan.target_home)),
            "plan": _ADAPTER.dump_python(plan, mode="json", warnings="error"),
        }
        _decode(
            document, database_url=database_url, target_home=plan.target_home, key=key
        )
        plaintext = _canonical(document)
        if len(plaintext) > max_bytes:
            raise MigrationError("Recovery plan exceeds the configured byte limit")
        with _read_only_target(database_url) as conn:
            assert_target_anchor(conn, target_anchor)
        with _parent(path) as (fd, name, resolved):
            if resolved.is_relative_to(plan.source.home) or resolved.is_relative_to(
                backup
            ):
                raise MigrationError(
                    "Recovery plan must be outside the source and independent backup"
                )
            try:
                existing = _read(fd, name, key, max_bytes)
            except FileNotFoundError:
                existing = None
            if existing is not None:
                if _canonical(existing) != plaintext:
                    raise MigrationError(
                        "Existing recovery plan differs; it will not be replaced"
                    )
                return "already_verified"
            temporary_name = f".migration-plan-{uuid4().hex}.tmp"
            try:
                file = os.open(
                    temporary_name,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                    0o600,
                    dir_fd=fd,
                )
                temporary = temporary_name
                with os.fdopen(file, "wb") as stream:
                    os.fchmod(stream.fileno(), 0o600)
                    stream.write(_MAGIC + cipher.encrypt(plaintext))
                    stream.flush()
                    os.fsync(stream.fileno())
                os.link(
                    temporary, name, src_dir_fd=fd, dst_dir_fd=fd, follow_symlinks=False
                )
                os.unlink(temporary, dir_fd=fd)
                temporary = None
                os.fsync(fd)
                if _canonical(_read(fd, name, key, max_bytes)) != plaintext:
                    raise MigrationError("Published recovery plan failed verification")
            finally:
                if temporary is not None:
                    os.unlink(temporary, dir_fd=fd)
        return "created_and_verified"
    except (OSError, ValueError, TypeError, KeyError, InvalidToken) as exc:
        if isinstance(exc, MigrationError):
            raise
        raise MigrationError(
            "Private recovery-plan publication failed; inspect the existing file before retrying, never replace it blindly"
        ) from None
