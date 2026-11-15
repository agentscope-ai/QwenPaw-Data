"""Read-only legacy Host inventory and verified, non-overwriting backups.

This module deliberately does not import settings, initialize SQL, decrypt
credentials, or run old store classes. Source writers must be stopped before
backup/import. Hash verification detects changes; it is not a filesystem freeze
or a defence against a malicious process controlling the source directory.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
import sys
import unicodedata
from collections import Counter
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any, BinaryIO, Iterator


class MigrationError(ValueError):
    """Safe-to-display migration failure; never include source document values."""


_CLI_LOG_PATHS = frozenset(
    {"host/qwenpaw_data.log", "host/qwenpaw_data.log.1", "host/qwenpaw_data.log.2"}
)


@dataclass(frozen=True)
class Limits:
    files: int = 100_000
    bytes: int = 10 * 1024**3
    metadata_bytes: int = 64 * 1024**2

    def __post_init__(self):
        if any(
            type(value) is not int or value <= 0
            for value in (self.files, self.bytes, self.metadata_bytes)
        ):
            raise MigrationError("Inventory limits must be positive")


def _json(data: bytes, label: str) -> Any:
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError("duplicate key")
            result[key] = value
        return result

    def constant(_value):
        raise ValueError("non-finite JSON value")

    try:
        return json.loads(data, object_pairs_hook=pairs, parse_constant=constant)
    except (ValueError, UnicodeError, RecursionError):
        raise MigrationError(f"Invalid JSON document: {label}") from None


def _canonical(value: Any) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
    ).encode()


def _signature(info: os.stat_result) -> tuple[int, ...]:
    # Windows Python 3.12+ reports creation time through stat().st_ctime_ns
    # but change time through fstat().st_ctime_ns (CPython #157671). Compare
    # creation time explicitly there; POSIX retains its change-time check.
    timestamp = (
        getattr(info, "st_birthtime_ns", info.st_ctime_ns)
        if sys.platform == "win32"
        else info.st_ctime_ns
    )
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, timestamp)


def _portable_key(value: str) -> str:
    # Filesystems may normalize Unicode as well as ignore case. Preserve the
    # original spelling, but never merge two identities or file paths by alias.
    return unicodedata.normalize("NFC", value).casefold()


def _chunks(stream: BinaryIO, size: int) -> Iterator[bytes]:
    copied = 0
    for chunk in iter(lambda: stream.read(min(1024 * 1024, size - copied + 1)), b""):
        copied += len(chunk)
        if copied > size:
            raise MigrationError("Source size changed during read")
        yield chunk
    if copied != size:
        raise MigrationError("Source size changed during read")


def _kind(relative: str) -> str:
    parts = PurePosixPath(relative).parts
    if len(parts) == 3 and parts[0] == "host" and parts[2].endswith(".json"):
        if parts[1] in {
            "sessions",
            "preferences",
            "cron",
            "settlement",
            "attachments",
            "feedback",
        }:
            return parts[1]
    if len(parts) == 4 and parts[:2] == ("host", "chats"):
        return {"chat.json": "chats", "events.jsonl": "events"}.get(
            parts[3], "archive_only"
        )
    if parts[:2] == ("host", "workspace"):
        if len(parts) >= 5 and parts[2] in {"uploads", "artifacts"}:
            return parts[2]
        if (
            len(parts) == 5
            and parts[2:4] == ("sessions", "console")
            and parts[4].endswith(".json")
        ):
            return "agent_state"
        if len(parts) > 4 and parts[2:4] == ("sessions", "dag"):
            return "dag_state"
        return "shared_workspace"
    return "archive_only"


@dataclass(frozen=True)
class SourceFile:
    path: str
    size: int
    sha256: str
    kind: str
    source_mode: int
    signature: tuple[int, ...] = field(repr=False, compare=False)

    def manifest(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "size": self.size,
            "sha256": self.sha256,
            "kind": self.kind,
            "source_mode": self.source_mode,
        }


@dataclass(frozen=True)
class Snapshot:
    home: Path
    files: tuple[SourceFile, ...]
    directories: tuple[str, ...]
    limits: Limits = field(repr=False)

    @property
    def digest(self) -> str:
        return hashlib.sha256(_canonical(self.manifest())).hexdigest()

    def manifest(self) -> dict[str, Any]:
        return {
            "format": "qwenpaw-data-legacy-host-backup",
            "version": 1,
            "directories": list(self.directories),
            "files": [f.manifest() for f in self.files],
        }


@contextmanager
def _read_file(
    home: Path, relative: str, expected: tuple[int, ...]
) -> Iterator[BinaryIO]:
    path = home / relative
    if path.is_symlink() or path.resolve() != path or not path.is_relative_to(home):
        raise MigrationError(
            f"Source path changed or traverses a symbolic link: {relative}"
        )
    fd = os.open(
        path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    )
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or _signature(info) != expected:
            raise MigrationError(f"Source changed during inventory: {relative}")
        yield stream
        after = os.fstat(stream.fileno())
        if (
            _signature(after) != expected
            # Always compare change time through the same descriptor API,
            # including on Windows where the portable signature uses birth time.
            or after.st_ctime_ns != info.st_ctime_ns
            or after.st_nlink != info.st_nlink
            or path.resolve() != path
            or _signature(path.lstat()) != expected
        ):
            raise MigrationError(f"Source changed during read: {relative}")


def scan_source(source_home: Path, *, limits: Limits = Limits()) -> Snapshot:
    """Hash every regular file under the explicitly selected legacy ``host`` root."""
    selected = Path(source_home).expanduser()
    if selected.is_symlink():
        raise MigrationError("Source home must not be a symbolic link")
    home = selected.resolve(strict=True)
    root = home / "host"
    if not root.is_dir() or root.is_symlink():
        raise MigrationError("Expected a real legacy <source-home>/host directory")
    files, directories, portable_names = [], [], set()
    total = 0

    def inaccessible(_error):
        raise MigrationError("Cannot enumerate all source directories") from None

    for folder, dirs, names in os.walk(root, followlinks=False, onerror=inaccessible):
        if len(directories) >= limits.files:
            raise MigrationError(
                "Source exceeds the configured directory inventory limits"
            )
        if Path(folder).resolve() != Path(folder):
            raise MigrationError(
                "Source directory changed or traverses a symbolic link"
            )
        dirs.sort()
        for name in [*dirs, *sorted(names)]:
            path = Path(folder) / name
            relative = path.relative_to(home).as_posix()
            if "\\" in relative or _portable_key(relative) in portable_names:
                raise MigrationError(
                    f"Non-portable or case-colliding source path: {relative}"
                )
            portable_names.add(_portable_key(relative))
            info = path.lstat()
            if stat.S_ISLNK(info.st_mode):
                raise MigrationError(
                    f"Symbolic links require explicit resolution: {relative}"
                )
            if stat.S_ISDIR(info.st_mode):
                continue
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise MigrationError(f"Expected a non-linked regular file: {relative}")
            total += info.st_size
            if len(files) >= limits.files or total > limits.bytes:
                raise MigrationError("Source exceeds the configured inventory limits")
            digest = hashlib.sha256()
            with _read_file(home, relative, _signature(info)) as stream:
                for chunk in _chunks(stream, info.st_size):
                    digest.update(chunk)
            files.append(
                SourceFile(
                    relative,
                    info.st_size,
                    digest.hexdigest(),
                    _kind(relative),
                    stat.S_IMODE(info.st_mode),
                    _signature(info),
                )
            )
        directories.append(Path(folder).relative_to(home).as_posix())
    return Snapshot(
        home,
        tuple(sorted(files, key=lambda f: f.path)),
        tuple(sorted(directories)),
        limits,
    )


def read_metadata(snapshot: Snapshot, source: SourceFile) -> Any:
    if source.size > snapshot.limits.metadata_bytes:
        raise MigrationError(
            f"Metadata exceeds the configured size limit: {source.path}"
        )
    with _read_file(snapshot.home, source.path, source.signature) as stream:
        data = stream.read(snapshot.limits.metadata_bytes + 1)
    if len(data) != source.size or hashlib.sha256(data).hexdigest() != source.sha256:
        raise MigrationError(f"Source changed after inventory: {source.path}")
    if source.kind == "events":
        return [_json(line, source.path) for line in data.splitlines() if line.strip()]
    return _json(data, source.path)


def _component(value: Any) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or value in {".", ".."}
        or value.endswith(".")
        or any(ord(c) < 32 or c in '/\\<>:"|?*' for c in value)
        or value.split(".")[0].upper()
        in {
            "CON",
            "PRN",
            "AUX",
            "NUL",
            *(f"COM{i}" for i in range(1, 10)),
            *(f"LPT{i}" for i in range(1, 10)),
        }
    ):
        raise MigrationError("Unsafe identity or path component")
    return value


def identity_mapping(data: Any) -> dict[str, dict[str, str]]:
    if (
        not isinstance(data, dict)
        or set(data) != {"version", "users"}
        or type(data["version"]) is not int
        or data["version"] != 1
    ):
        raise MigrationError("Identity mapping requires version=1 and a users object")
    users = data["users"]
    if not isinstance(users, dict):
        raise MigrationError("Identity mapping users must be an object")
    mapped, new_users = {}, set()
    for old_user, target in users.items():
        if not isinstance(old_user, str) or not old_user.strip():
            raise MigrationError("Legacy user IDs must be non-empty strings")
        if not isinstance(target, dict) or set(target) != {
            "user_id",
            "tenant_id",
            "workspace_id",
        }:
            raise MigrationError(
                "Each user mapping requires explicit user_id, tenant_id and workspace_id"
            )
        checked = {key: _component(value) for key, value in target.items()}
        # Preferences are keyed globally by user_id. Do not merge credentials
        # just because the target workspace happens to differ.
        if _portable_key(checked["user_id"]) in new_users:
            raise MigrationError(
                "Multiple legacy users cannot map to the same target user_id"
            )
        new_users.add(_portable_key(checked["user_id"]))
        mapped[old_user] = checked
    return mapped


def load_identity_mapping(path: Path) -> dict[str, dict[str, str]]:
    path = Path(path)
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 1024**2:
        raise MigrationError("Identity mapping must be a regular JSON file under 1 MiB")
    home = path.parent.resolve()
    with _read_file(home, path.name, _signature(path.lstat())) as stream:
        data = stream.read(1024**2 + 1)
    if len(data) > 1024**2:
        raise MigrationError("Identity mapping exceeds 1 MiB")
    return identity_mapping(_json(data, "identity mapping"))


@dataclass(frozen=True)
class Finding:
    code: str
    path: str


@dataclass
class Inspection:
    snapshot: Snapshot
    records: dict[str, list[tuple[str, Any]]] = field(repr=False)
    required_user_ids: set[str]
    findings: list[Finding]

    def report(self) -> dict[str, Any]:
        return {
            "stage": "inspection_only",
            "sql_written": False,
            "import_readiness": "not_evaluated",
            "source_digest": self.snapshot.digest,
            "files": len(self.snapshot.files),
            "bytes": sum(f.size for f in self.snapshot.files),
            "file_kinds": dict(
                sorted(Counter(f.kind for f in self.snapshot.files).items())
            ),
            "required_user_ids": sorted(self.required_user_ids),
            "findings": [{"code": f.code, "path": f.path} for f in self.findings],
        }


def inspect_source(
    source_home: Path,
    *,
    users: dict[str, dict[str, str]] | None = None,
    limits: Limits = Limits(),
) -> Inspection:
    snapshot = scan_source(source_home, limits=limits)
    if users is not None:
        users = identity_mapping({"version": 1, "users": users})
    records: dict[str, list[tuple[str, Any]]] = {}
    findings, owners = [], set()
    sessions, chats, attachments = {}, {}, {}

    def finding(code, path):
        findings.append(Finding(code, path))

    def owner(doc, path):
        value = doc.get("user_id")
        if isinstance(doc.get("identity"), dict):
            value = doc["identity"].get("user_id")
        if not isinstance(value, str) or not value.strip():
            raise MigrationError(f"Missing legacy ownership: {path}")
        owners.add(value)
        if users is None or value not in users:
            finding("identity_mapping_required", path)
        return value

    file_paths = {f.path for f in snapshot.files}
    for source in snapshot.files:
        kind, path = source.kind, source.path
        if kind in {"archive_only", "shared_workspace", "dag_state"}:
            # Included byte-for-byte in backup, but must be accounted for by an
            # importer; an inventory is not permission to discard these files.
            name = PurePosixPath(path).name
            locked = (
                PurePosixPath(path).with_name(name[1:-5]).as_posix()
                if name.startswith(".") and name.endswith(".lock") and len(name) > 6
                else ""
            )
            empty_known_lock = (
                source.size == 0
                and locked in file_paths
                and _kind(locked)
                in {
                    "sessions",
                    "preferences",
                    "chats",
                    "events",
                    "cron",
                    "settlement",
                    "attachments",
                    "feedback",
                    "agent_state",
                }
            )
            if not empty_known_lock and path not in _CLI_LOG_PATHS:
                finding("explicit_file_disposition_required", path)
            continue
        if kind in {"artifacts", "uploads"}:
            continue
        doc = read_metadata(snapshot, source)
        records.setdefault(kind, []).append((path, doc))
        if kind in {"events", "feedback"}:
            if not isinstance(doc, list) or any(
                not isinstance(row, dict) for row in doc
            ):
                raise MigrationError(f"Expected a list of records: {path}")
            if kind == "feedback":
                for row in doc:
                    owner(row, path)
            continue
        if not isinstance(doc, dict):
            raise MigrationError(f"Expected an object: {path}")
        if kind == "preferences":
            owner({"user_id": PurePosixPath(path).stem}, path)
        elif kind == "agent_state":
            finding("agent_state_mapping_required", path)
        else:
            who = owner(doc, path)
            ident = _component(doc.get("id"))
            expected = (
                PurePosixPath(path).parent.name
                if kind == "chats"
                else PurePosixPath(path).stem
            )
            if ident != expected:
                raise MigrationError(f"Record ID disagrees with its filename: {path}")
            if kind in {"sessions", "chats", "attachments"}:
                index = {
                    "sessions": sessions,
                    "chats": chats,
                    "attachments": attachments,
                }[kind]
                index[ident] = (doc, who, path)
            if kind == "sessions":
                _component(doc.get("agent_id", "default"))

    def session_ref(doc, who, path):
        parent = sessions.get(_component(doc.get("session_id")))
        if parent is None or parent[1] != who:
            finding("missing_or_cross_owner_session", path)
            return False
        return True

    sequences: set[tuple[str, int]] = set()
    for doc, who, path in chats.values():
        session_ref(doc, who, path)
        seq = doc.get("sequence")
        if type(seq) is not int or seq < 1 or (doc["session_id"], seq) in sequences:
            finding("invalid_or_duplicate_chat_sequence", path)
        else:
            sequences.add((doc["session_id"], seq))
        refs = doc.get("attachments", [])
        if not isinstance(refs, list):
            raise MigrationError(f"Expected an attachment reference list: {path}")
        for ref in refs:
            item = (
                attachments.get(_component(ref.get("attachment_id")))
                if isinstance(ref, dict)
                else None
            )
            if (
                item is None
                or item[1] != who
                or item[0].get("session_id") != doc.get("session_id")
            ):
                finding("missing_or_cross_session_attachment", path)
    for doc, who, path in sessions.values():
        parent_id, fork_id = (
            doc.get("parent_session_id"),
            doc.get("forked_from_chat_id"),
        )
        if parent_id is not None:
            parent = sessions.get(_component(parent_id))
            if parent is None or parent[1] != who or parent_id == doc["id"]:
                finding("missing_or_cross_owner_parent_session", path)
        if fork_id is not None:
            origin = chats.get(_component(fork_id))
            if (
                origin is None
                or origin[1] != who
                or origin[0]["session_id"] != parent_id
            ):
                finding("missing_or_cross_session_fork_point", path)
        count = doc.get("chat_count", 0)
        max_seq = max((seq for sid, seq in sequences if sid == doc["id"]), default=0)
        if type(count) is not int or count < max_seq:
            finding("session_chat_count_requires_reconciliation", path)
    for doc, who, path in attachments.values():
        session_ref(doc, who, path)
        sid = _component(doc.get("session_id"))
        filename = _component(doc.get("filename"))
        expected = f"uploads/{sid}/{filename}"
        if doc.get("storage_path") != expected:
            finding("unsafe_attachment_storage_path", path)
        elif f"host/workspace/{expected}" not in file_paths:
            finding("missing_attachment_file", path)
    for source in snapshot.files:
        if (
            source.kind in {"uploads", "artifacts"}
            and PurePosixPath(source.path).parts[3] not in sessions
        ):
            finding("unowned_session_file", source.path)
    logged_chats = set()
    for path, events in records.get("events", []):
        cid = PurePosixPath(path).parent.name
        logged_chats.add(cid)
        chat = chats.get(cid)
        if chat is None:
            finding("orphan_event_log", path)
            continue
        for seq, event in enumerate(events):
            if (
                event.get("chat_id") != cid
                or event.get("session_id") != chat[0].get("session_id")
                or type(event.get("sequence_number")) is not int
                or event["sequence_number"] != seq
            ):
                finding("invalid_event_identity_or_sequence", path)
                break
        watermark = chat[0].get("last_sequence_number", -1)
        if type(watermark) is not int or watermark > len(events) - 1:
            finding("event_watermark_ahead_of_log", path)
        elif watermark < len(events) - 1:
            finding("event_watermark_requires_reconciliation", path)
    for cid, (doc, _, path) in chats.items():
        if cid not in logged_chats and doc.get("last_sequence_number", -1) != -1:
            finding("missing_event_log", path)
    for kind in ("cron", "settlement", "feedback"):
        for path, value in records.get(kind, []):
            for doc in value if isinstance(value, list) else [value]:
                who = owner(doc, path)
                if doc.get("session_id") is not None:
                    session_ref(doc, who, path)
                cid = (
                    doc.get("source_chat_id")
                    if kind == "settlement"
                    else doc.get("chat_id")
                )
                if cid is not None:
                    _component(cid)
                if kind == "feedback" and cid != PurePosixPath(path).stem:
                    finding("feedback_filename_chat_mismatch", path)
                if cid is not None and (
                    cid not in chats
                    or chats[cid][1] != who
                    or chats[cid][0].get("session_id") != doc.get("session_id")
                ):
                    finding("missing_or_cross_session_chat", path)
    return Inspection(
        snapshot, records, owners, sorted(set(findings), key=lambda f: (f.path, f.code))
    )


_MANIFEST = "migration-manifest.json"
_INCOMPLETE = ".incomplete"


def _check_backup_platform(platform: str, version: tuple[int, int, int]) -> None:
    # Windows mkdir(0700) gained a private DACL in these maintenance releases:
    # https://docs.python.org/3.11/library/os.html#os.mkdir
    # https://docs.python.org/3.12/library/os.html#os.mkdir
    supported = (3, 11, 10) <= version < (3, 12, 0) or version >= (3, 12, 4)
    if platform == "win32" and not supported:
        raise MigrationError(
            "Private Windows backups require Python 3.11.10+ or 3.12.4+; upgrade Python before copying credentials"
        )


def _check_private_directory(directory: Path) -> None:
    if os.name == "posix" and directory.stat().st_mode & 0o077:
        raise MigrationError(
            "Backup directory permissions allow access by other users; no credentials will be copied"
        )


def _same_contents(left: Snapshot, right: Snapshot) -> bool:
    # Backups intentionally use private modes. Original file modes remain in
    # the manifest, including executable bits needed by custom skill scripts.
    return left.directories == right.directories and [
        (f.path, f.size, f.sha256, f.kind) for f in left.files
    ] == [(f.path, f.size, f.sha256, f.kind) for f in right.files]


def verify_backup(destination: Path, expected: Snapshot) -> None:
    destination = Path(destination).resolve(strict=True)
    _check_private_directory(destination)
    manifest = destination / _MANIFEST
    if (
        manifest.is_symlink()
        or (destination / _INCOMPLETE).exists()
        or not manifest.is_file()
    ):
        raise MigrationError("Backup is incomplete or has an unsafe manifest")
    expected_bytes = _canonical(expected.manifest()) + b"\n"
    with _read_file(destination, _MANIFEST, _signature(manifest.lstat())) as stream:
        actual = stream.read(len(expected_bytes) + 1)
    if actual != expected_bytes:
        raise MigrationError(
            "Backup manifest does not match the selected source snapshot"
        )
    if {p.name for p in destination.iterdir()} != {"host", _MANIFEST}:
        raise MigrationError("Backup contains unexpected top-level entries")
    if not _same_contents(scan_source(destination, limits=expected.limits), expected):
        raise MigrationError("Backup content verification failed")


def create_backup(
    snapshot: Snapshot, destination: Path, *, source_stopped: bool = False
) -> str:
    """Create a private verified backup; never overwrite or remove existing data.

    On failure the new directory remains marked .incomplete for inspection.
    Repeated invocation verifies an existing completed backup instead of writing.
    """
    if not source_stopped:
        raise MigrationError(
            "Stop all source writers and explicitly confirm source_stopped"
        )
    _check_backup_platform(sys.platform, sys.version_info[:3])
    destination = Path(destination).expanduser()
    if destination.is_symlink():
        raise MigrationError("Backup destination must not be a symbolic link")
    destination = destination.resolve()
    if destination.is_relative_to(snapshot.home) or snapshot.home.is_relative_to(
        destination
    ):
        raise MigrationError("Backup destination must be disjoint from the source home")
    if (
        scan_source(snapshot.home, limits=snapshot.limits).manifest()
        != snapshot.manifest()
    ):
        raise MigrationError("Source changed since inventory; create a new inspection")
    if destination.exists():
        verify_backup(destination, snapshot)
        return "already_verified"
    destination.mkdir(
        mode=0o700
    )  # Parent must exist; never create a broad directory tree.
    _check_private_directory(destination)
    marker = destination / _INCOMPLETE
    marker.touch(mode=0o600, exist_ok=False)
    try:
        for relative in snapshot.directories:
            (destination / relative).mkdir(mode=0o700)
        for source in snapshot.files:
            with _read_file(snapshot.home, source.path, source.signature) as stream:
                fd = os.open(
                    destination / source.path,
                    os.O_CREAT | os.O_EXCL | os.O_WRONLY,
                    0o600,
                )
                with os.fdopen(fd, "wb") as output:
                    for chunk in _chunks(stream, source.size):
                        output.write(chunk)
                    output.flush()
                    os.fsync(output.fileno())
        if (
            scan_source(snapshot.home, limits=snapshot.limits).manifest()
            != snapshot.manifest()
        ):
            raise MigrationError("Source changed during backup")
        if not _same_contents(
            scan_source(destination, limits=snapshot.limits), snapshot
        ):
            raise MigrationError("Copied bytes do not match the source inventory")
        fd = os.open(
            destination / _MANIFEST, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600
        )
        with os.fdopen(fd, "wb") as output:
            output.write(_canonical(snapshot.manifest()) + b"\n")
            output.flush()
            os.fsync(output.fileno())
        marker.unlink()  # Only our completion marker; never source or backup contents.
        verify_backup(destination, snapshot)
        return "created_and_verified"
    except Exception:
        if not marker.exists():
            marker.touch(mode=0o600, exist_ok=False)
        raise MigrationError(
            f"Backup did not complete; partial data retained at {destination}"
        ) from None
