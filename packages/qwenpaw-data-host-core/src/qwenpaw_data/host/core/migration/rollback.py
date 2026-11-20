"""Recoverable, stopped-writer rollback of one authenticated saved import.

Only positively receipted SQL rollback permits unlinking an unchanged owned
publication. Backup, staged data, original/rollback receipts and directories are
retained. This is an internal POSIX entry point, not a general database restore.
"""

from dataclasses import dataclass
import os
from pathlib import Path

from cryptography.fernet import InvalidToken

from .bundle import load_recovery_plan, _absolute
from .legacy_json import MigrationError, _canonical, verify_backup
from .locking import migration_lock
from .sql_import import recover_sql_receipt, _run_id
from .sql_rollback import read_sql_rollback_receipt, rollback_sql_rows
from . import publication, staging


@dataclass(frozen=True)
class RollbackReceipt:
    run_id: str
    status: str
    plan_digest: str
    data_digest: str
    rows: int
    files: int

    def report(self):
        return {
            **vars(self),
            "stage": "coordinated_sql_file_rollback",
            "imported_rows_absent": True,
            "published_files_absent": True,
            "forward_replay_forbidden": True,
            "backup_staging_journal_and_directories_retained": True,
            "external_dependencies_checked": False,
        }


def _document(recovery, data_digest, action):
    return {
        **publication._base(recovery),
        "action": action,
        "target_anchor": recovery.target_anchor,
        "database_binding": recovery.database_binding,
        "home_identity": list(recovery.home_identity),
        "data_digest": data_digest,
        "rows": len(recovery.plan.rows),
        "files": len(recovery.plan.files),
    }


def _record(run, recovery, digest, cipher, *, required):
    intent = publication._read_optional(run, "rollback-intent.enc", cipher)
    complete = publication._read_optional(run, "rollback-complete.enc", cipher)
    if intent is None and (required or complete is not None):
        raise MigrationError("SQL rollback has no authenticated local rollback intent")
    if intent is not None and _canonical(intent) != _canonical(
        _document(recovery, digest, "rollback_intent")
    ):
        raise MigrationError("Rollback intent differs from the bound import")
    if complete is not None and _canonical(complete) != _canonical(
        _document(recovery, digest, "rollback_complete")
    ):
        raise MigrationError("Rollback completion record differs from the bound import")
    return complete is not None


def _files(run, recovery, files, lock, cipher, *, required):
    publication._directories(run, recovery, lock, cipher, create=False)
    states = []
    for item in files:
        if (
            publication._read_optional(
                run, f"publish-file-{item.index:08d}.enc", cipher
            )
            is None
        ):
            raise MigrationError("Rollback requires every original publication intent")
        states.append(
            publication._file_state(
                run, recovery, item, lock, cipher, required=required
            )
        )
    return states


def _intent(recovery, lock, url, key, digest):
    # Called inside SQL rollback, before commit. No SQL connection or unlink here.
    with publication._prepared(recovery, lock, url, key, allow_rollback=True) as (
        run,
        files,
        cipher,
    ):
        if _record(run, recovery, digest, cipher, required=False):
            raise MigrationError("Completed rollback cannot start a fresh SQL rollback")
        _files(run, recovery, files, lock, cipher, required=True)
        publication._once(
            run,
            "rollback-intent.enc",
            _document(recovery, digest, "rollback_intent"),
            cipher,
        )


def _remove_publication(recovery, lock, url, key, sql, limits):
    with publication._prepared(recovery, lock, url, key, allow_rollback=True) as (
        run,
        files,
        cipher,
    ):
        complete = _record(run, recovery, sql.data_digest, cipher, required=True)
        states = _files(run, recovery, files, lock, cipher, required=False)
        if complete and any(states):
            raise MigrationError("A completed rollback publication appeared again")
        # Reconfirm the exact database outcome AFTER file preflight. No missing or
        # inaccessible receipt, reused PK or new dependency authorizes unlink.
        confirmed = read_sql_rollback_receipt(
            recovery.plan,
            database_url=url,
            run_id=recovery.run_id,
            target_anchor=recovery.target_anchor,
            target_stopped=True,
            recover_sqlite=True,
            **limits,
        )
        if confirmed is None or confirmed.data_digest != sql.data_digest:
            raise MigrationError("SQL rollback outcome is not positively confirmed")
        for item in files:
            lock.check()
            exists = publication._file_state(
                run, recovery, item, lock, cipher, required=False
            )
            # Rehash immediately before unlink, not only at batch preflight.
            staging._completed(
                run,
                item.path.parent.name,
                item.index,
                recovery,
                staging._cipher(key),
                item.path.parent.parent,
                links=2 if exists else 1,
            )
            relative = Path(recovery.plan.files[item.index].destination)
            with publication._walk(lock, relative.parent) as parent:
                expected = publication._intent(recovery, item, parent)
                actual = publication._read_optional(
                    run, f"publish-file-{item.index:08d}.enc", cipher
                )
                if _canonical(expected) != _canonical(actual):
                    raise MigrationError("Publication parent changed before unlink")
                if exists:
                    current = os.stat(
                        relative.name, dir_fd=parent, follow_symlinks=False
                    )
                    if (current.st_dev, current.st_ino) != (item.device, item.inode):
                        raise MigrationError("Publication inode changed before unlink")
                    # All application writers MUST remain stopped; the home lock
                    # only serializes cooperating migration invocations.
                    os.unlink(relative.name, dir_fd=parent)
                os.fsync(parent)  # Also persist an interrupted prior unlink.
        if any(_files(run, recovery, files, lock, cipher, required=False)):
            raise MigrationError("Rollback publication remains unexpectedly present")
        publication._once(
            run,
            "rollback-complete.enc",
            _document(recovery, sql.data_digest, "rollback_complete"),
            cipher,
        )
        return complete


def rollback_saved_plan(
    path: Path,
    *,
    database_url: str,
    target_home: Path,
    key: bytes,
    target_stopped: bool = False,
    expected_run_id: str | None = None,
    **limits,
) -> RollbackReceipt:
    """Rollback/recover the SAME saved import; never replan or delete directories.

    Source may be offline, but independent backup and staging must still verify.
    A failed/ambiguous SQL commit retains every file; retry resolves its durable
    receipt. Known native/JSON dependencies are checked, not external consumers.
    """
    if target_stopped is not True:
        raise MigrationError("Coordinated rollback requires all target writers stopped")
    selected = Path(target_home)
    _absolute(selected)
    if selected.is_symlink():
        raise MigrationError("Target home must not be a final symbolic link")
    try:
        home = selected.resolve(strict=True)
        initial = load_recovery_plan(
            path, database_url=database_url, target_home=home, key=key
        )
        if expected_run_id is not None:
            _run_id(expected_run_id)
            if initial.run_id != expected_run_id:
                raise MigrationError(
                    "Confirmed run ID differs from the saved rollback plan"
                )
        with migration_lock(home, target_stopped=True) as lock:
            recovery = load_recovery_plan(
                path, database_url=database_url, target_home=home, key=key
            )
            if (initial.run_id, initial.digest) != (recovery.run_id, recovery.digest):
                raise MigrationError("Saved recovery plan changed before locking")
            verify_backup(recovery.backup, recovery.plan.source)
            sql = read_sql_rollback_receipt(
                recovery.plan,
                database_url=database_url,
                run_id=recovery.run_id,
                target_anchor=recovery.target_anchor,
                target_stopped=True,
                recover_sqlite=True,
                **limits,
            )
            if sql is None:
                original = recover_sql_receipt(
                    recovery.plan,
                    database_url=database_url,
                    run_id=recovery.run_id,
                    target_anchor=recovery.target_anchor,
                    target_stopped=True,
                )
                if original is None:
                    raise MigrationError("Only a committed import can be rolled back")
                # Read-only preflight before any provisional SQL DELETE.
                with publication._prepared(
                    recovery, lock, database_url, key, allow_rollback=True
                ) as (run, files, cipher):
                    if _record(
                        run, recovery, original.data_digest, cipher, required=False
                    ):
                        raise MigrationError("Completed rollback lost its SQL receipt")
                    _files(run, recovery, files, lock, cipher, required=True)
                sql = rollback_sql_rows(
                    recovery.plan,
                    database_url=database_url,
                    run_id=recovery.run_id,
                    target_anchor=recovery.target_anchor,
                    target_stopped=True,
                    recover_sqlite=True,
                    before_commit=lambda: _intent(
                        recovery, lock, database_url, key, original.data_digest
                    ),
                    **limits,
                )
            complete = _remove_publication(
                recovery, lock, database_url, key, sql, limits
            )
            return RollbackReceipt(
                recovery.run_id,
                "already_rolled_back" if complete else "rolled_back",
                sql.plan_digest,
                sql.data_digest,
                sql.row_count,
                sql.file_count,
            )
    except (OSError, ValueError, TypeError, InvalidToken) as exc:
        if isinstance(exc, MigrationError):
            raise
        raise MigrationError(
            "Coordinated rollback could not finish; retain backup, staging and journal and retry only the same saved plan"
        ) from None
