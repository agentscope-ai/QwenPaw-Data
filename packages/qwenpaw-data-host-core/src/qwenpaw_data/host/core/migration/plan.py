"""Read-only whole-source preparation, not an executable import manifest.

Requires a verified backup and stopped writers. Rows and proposed scoped file
placements stay private in memory; the report contains counts and safe issues.
Database conflicts, target Auth membership, transactional execution and rollback
are separate gates. A conversion-complete report does not authorize import.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Any

from qwenpaw_data.host.core.db.tables import Base
from qwenpaw_data.host.core.providers.registry import ProviderRegistry

from .credentials import CredentialRewriter
from .events import convert_events
from .legacy_json import (
    Limits,
    MigrationError,
    Snapshot,
    _CLI_LOG_PATHS,
    _component,
    _portable_key,
    identity_mapping,
    inspect_source,
    scan_source,
    verify_backup,
)
from .preferences import convert_preferences
from .records import PreparedRecord, convert_record


@dataclass(frozen=True, order=True)
class PlanIssue:
    code: str
    path: str
    message: str = ""


@dataclass(frozen=True)
class FilePlacement:
    source: str
    destination: str  # Relative to the explicitly selected target home.
    size: int
    sha256: str
    source_mode: int


@dataclass(frozen=True)
class TargetIdentity:
    user_id: str
    tenant_id: str
    workspace_id: str


@dataclass(frozen=True)
class MigrationPlan:
    source: Snapshot = field(repr=False)
    target_home: Path
    prepared_at: datetime
    rows: tuple[PreparedRecord, ...] = field(repr=False)
    files: tuple[FilePlacement, ...] = field(repr=False)
    directories: tuple[str, ...] = field(repr=False)
    issues: tuple[PlanIssue, ...]
    notices: tuple[PlanIssue, ...]
    identities: tuple[TargetIdentity, ...] = field(repr=False, default=())

    def report(self) -> dict[str, Any]:
        return {
            "stage": "read_only_preparation",
            "source_digest": self.source.digest,
            "source_files": len(self.source.files),
            "prepared_at": self.prepared_at.isoformat(),
            "target_home": str(self.target_home),
            "conversion_complete": not self.issues,
            "import_ready": False,
            "target_database_checked": False,
            "target_identity_membership_checked": False,
            "target_master_key_provisioned": False,
            "sql_written": False,
            "target_files_written": False,
            "prepared_rows": dict(
                sorted(Counter(row.table for row in self.rows).items())
            ),
            "proposed_files": len(self.files),
            "proposed_directories": len(self.directories),
            "issues": [vars(issue) for issue in self.issues],
            "notices": [vars(notice) for notice in self.notices],
            "warning": "Read-only conversion plan, not an executable import manifest. Recheck target conflicts and identity membership before import. Keep source/target master keys separately; environment secrets are not included in the backup or provisioned by planning.",
        }


def prepare_migration(
    source_home: Path,
    *,
    backup: Path,
    target_home: Path,
    users: dict[str, dict[str, str]],
    credentials: CredentialRewriter,
    registry: ProviderRegistry,
    prepared_at: datetime,
    target_context_trigger_ratio: float,
    target_context_reserve_ratio: float,
    model_context_sizes: dict[str, dict[str, int]] | None = None,
    disable_cron: bool = False,
    source_stopped: bool = False,
    limits: Limits = Limits(),
) -> MigrationPlan:
    if source_stopped is not True:
        raise MigrationError("Migration planning requires stopped source writers")
    if type(disable_cron) is not bool:
        raise MigrationError("Cron disabling must be an explicit boolean")
    if not isinstance(registry, ProviderRegistry) or not isinstance(
        credentials, CredentialRewriter
    ):
        raise MigrationError(
            "Explicit target catalog and credential rewriter are required"
        )
    if (
        any(
            type(value) not in (float, int)
            for value in (target_context_trigger_ratio, target_context_reserve_ratio)
        )
        or not 0 < target_context_reserve_ratio < target_context_trigger_ratio <= 0.9
    ):
        raise MigrationError(
            "Explicit target context ratios require 0 < reserve < trigger <= 0.9"
        )
    if (
        not isinstance(prepared_at, datetime)
        or prepared_at.tzinfo is None
        or prepared_at.utcoffset() is None
    ):
        raise MigrationError("Migration plan requires an explicit aware timestamp")
    users = identity_mapping({"version": 1, "users": users})
    inspection = inspect_source(source_home, users=users, limits=limits)
    snapshot = inspection.snapshot
    backup_path = Path(backup).resolve()
    if backup_path.is_relative_to(snapshot.home) or snapshot.home.is_relative_to(
        backup_path
    ):
        raise MigrationError("Source and verified backup must be disjoint")
    verify_backup(backup_path, snapshot)
    selected = Path(target_home).expanduser()
    if selected.is_symlink():
        raise MigrationError("Target home must not be a symbolic link")
    target = selected.resolve()
    for protected in (snapshot.home, backup_path):
        if target.is_relative_to(protected) or protected.is_relative_to(target):
            raise MigrationError("Source, backup and target home must be disjoint")
    if target.exists() and not target.is_dir():
        raise MigrationError("Target home must be a directory or a new path")
    sizes = {} if model_context_sizes is None else model_context_sizes
    if not isinstance(sizes, dict) or any(
        not isinstance(key, str) or not isinstance(value, dict)
        for key, value in sizes.items()
    ):
        raise MigrationError("Model context sizes require a per-legacy-user mapping")
    pref_users = {
        PurePosixPath(path).stem
        for path, _ in inspection.records.get("preferences", [])
    }
    if set(sizes) - pref_users:
        raise MigrationError(
            "Context-size mapping includes users without legacy preferences"
        )
    issues = [PlanIssue(f.code, f.path) for f in inspection.findings]
    notices: list[PlanIssue] = []
    rows, primary_keys = [], set()
    sessions, chats = {}, {}

    def add(row, path):
        table = Base.metadata.tables[row.table]
        key = (row.table, *(row.values[col.name] for col in table.primary_key.columns))
        if key in primary_keys:
            issues.append(PlanIssue("duplicate_target_primary_key", path))
            return
        primary_keys.add(key)
        rows.append(row)
        notices.extend(PlanIssue(code, path) for code in row.notices)

    for kind in ("sessions", "chats", "attachments", "settlement", "feedback", "cron"):
        for path, value in inspection.records.get(kind, []):
            for document in value if kind == "feedback" else [value]:
                try:
                    row = convert_record(
                        kind, document, users=users, disable_cron=disable_cron
                    )
                except MigrationError as exc:
                    issues.append(PlanIssue("record_conversion_failed", path, str(exc)))
                    continue
                add(row, path)
                if kind == "sessions":
                    sessions[row.values["id"]] = row
                elif kind == "chats":
                    chats[row.values["id"]] = row
    for path, document in inspection.records.get("preferences", []):
        who = PurePosixPath(path).stem
        try:
            converted = convert_preferences(
                document,
                old_user_id=who,
                users=users,
                credentials=credentials,
                registry=registry,
                converted_at=prepared_at,
                target_context_trigger_ratio=target_context_trigger_ratio,
                target_context_reserve_ratio=target_context_reserve_ratio,
                model_context_sizes=sizes.get(who),
            )
        except MigrationError as exc:
            issues.append(PlanIssue("preference_conversion_failed", path, str(exc)))
            continue
        for row in converted:
            add(row, path)
    logs = {
        PurePosixPath(path).parent.name: (path, events)
        for path, events in inspection.records.get("events", [])
    }
    for cid, chat in chats.items():
        path, events = logs.get(cid, (f"host/chats/{cid}/events.jsonl", []))
        try:
            converted = convert_events(events, chat=chat)
        except MigrationError as exc:
            issues.append(PlanIssue("event_conversion_failed", path, str(exc)))
            continue
        for row in converted:
            add(row, path)

    paths: dict[str, tuple[str, bool]] = {}
    existing_names: dict[Path, dict[str, set[str]]] = {}
    directories, files = set(), []
    placements: dict[str, FilePlacement] = {}

    def destination(relative):
        parts = PurePosixPath(relative).parts
        sid = parts[3]
        session = sessions.get(sid)
        if session is None:
            raise MigrationError("File or directory has no converted owning session")
        value = session.values
        components = [
            value[k] for k in ("tenant_id", "workspace_id", "user_id", "agent_id")
        ]
        components += ["workspace", *parts[2:]]
        return "/".join(_component(part) for part in components)

    def claim(relative, is_directory):
        if target.resolve() != target or target.is_symlink():
            raise MigrationError("Target home changed or traverses a symbolic link")
        parts = PurePosixPath(relative).parts
        for index in range(1, len(parts) + 1):
            path = "/".join(parts[:index])
            directory = index < len(parts) or is_directory
            folded = _portable_key(path)
            if folded in paths and paths[folded] != (path, directory):
                raise MigrationError(
                    "Proposed target paths collide by case or file/directory type"
                )
            paths[folded] = (path, directory)
            existing = target / path
            parent = existing.parent
            if parent.is_dir() and not parent.is_symlink():
                if parent not in existing_names:
                    names: dict[str, set[str]] = {}
                    for count, child in enumerate(parent.iterdir()):
                        if count >= limits.files:
                            raise MigrationError(
                                "Target directory exceeds the configured inventory limit"
                            )
                        names.setdefault(_portable_key(child.name), set()).add(
                            child.name
                        )
                    existing_names[parent] = names
                spellings = existing_names[parent].get(
                    _portable_key(existing.name), set()
                )
                if spellings and spellings != {existing.name}:
                    raise MigrationError("Target contains a case-colliding path")
            if existing.is_symlink():
                raise MigrationError("Proposed target path traverses a symbolic link")
            if existing.exists() and (not directory or not existing.is_dir()):
                raise MigrationError(
                    "Proposed target path already contains a conflicting entry"
                )
            if directory:
                directories.add(path)

    # Claim every future agent workspace, even if it currently has no files.
    # Otherwise case/Unicode aliases could first collide after migration.
    for sid, session in sessions.items():
        prefix = "/".join(
            [
                *(
                    session.values[key]
                    for key in ("tenant_id", "workspace_id", "user_id", "agent_id")
                ),
                "workspace",
            ]
        )
        try:
            claim(prefix, True)
        except MigrationError as exc:
            issues.append(
                PlanIssue(
                    "workspace_placement_failed", f"host/sessions/{sid}.json", str(exc)
                )
            )
    for relative in snapshot.directories:
        parts = PurePosixPath(relative).parts
        if (
            len(parts) >= 4
            and parts[:2] == ("host", "workspace")
            and parts[2] in {"uploads", "artifacts"}
        ):
            try:
                claim(destination(relative), True)
            except MigrationError as exc:
                issues.append(
                    PlanIssue("directory_placement_failed", relative, str(exc))
                )
    for file in snapshot.files:
        if file.kind not in {"uploads", "artifacts"}:
            if file.path in _CLI_LOG_PATHS:
                notices.append(
                    PlanIssue("historical_cli_log_retained_in_backup", file.path)
                )
            if file.path.endswith(".lock") and not any(
                issue.path == file.path for issue in issues
            ):
                notices.append(PlanIssue("empty_lock_retained_in_backup", file.path))
            continue
        try:
            dest = destination(file.path)
            claim(dest, False)
        except MigrationError as exc:
            issues.append(PlanIssue("file_placement_failed", file.path, str(exc)))
            continue
        placement = FilePlacement(
            file.path, dest, file.size, file.sha256, file.source_mode
        )
        files.append(placement)
        placements[file.path] = placement

    def scoped_file(row, relative):
        sid = row.values["session_id"]
        session = sessions.get(sid)
        if session is None or relative not in placements:
            return False
        expected_prefix = "/".join(
            [
                *(
                    session.values[key]
                    for key in ("tenant_id", "workspace_id", "user_id", "agent_id")
                ),
                "workspace",
                "",
            ]
        )
        return placements[relative].destination.startswith(expected_prefix)

    artifacts = {row.values["id"]: row for row in rows if row.table == "artifacts"}
    for row in rows:
        values = row.values
        if row.table in {"attachments", "artifacts"}:
            relative = (
                "host/workspace/"
                + values["storage_path" if row.table == "attachments" else "path"]
            )
            if not scoped_file(row, relative):
                issues.append(
                    PlanIssue("missing_or_unscoped_referenced_file", relative)
                )
        elif row.table == "feedbacks" and values["artifact_ref_json"] is not None:
            artifact = artifacts.get(values["artifact_ref_json"]["artifact_id"])
            if artifact is None or any(
                artifact.values[key] != values[key]
                for key in ("user_id", "tenant_id", "workspace_id", "session_id")
            ):
                issues.append(
                    PlanIssue(
                        "missing_or_cross_scope_feedback_artifact",
                        f"host/feedback/{values['chat_id']}.json",
                    )
                )
        elif row.table == "chats":
            for comment in values["artifact_comments_json"]:
                if not scoped_file(row, "host/workspace/" + comment["path"]):
                    issues.append(
                        PlanIssue(
                            "missing_or_unscoped_commented_file",
                            f"host/chats/{values['id']}/chat.json",
                        )
                    )
    # Do not report a plan for a source changed while its records were converted.
    if scan_source(snapshot.home, limits=limits).digest != snapshot.digest:
        raise MigrationError("Source changed during migration planning")
    return MigrationPlan(
        snapshot,
        target,
        prepared_at,
        tuple(rows),
        tuple(files),
        tuple(sorted(directories)),
        tuple(sorted(set(issues))),
        tuple(sorted(set(notices))),
        tuple(
            TargetIdentity(**users[user_id])
            for user_id in sorted(inspection.required_user_ids)
            if user_id in users
        ),
    )
