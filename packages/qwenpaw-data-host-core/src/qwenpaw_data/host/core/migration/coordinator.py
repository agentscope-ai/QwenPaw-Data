"""Apply/recover one saved plan under a target-home lock, without implicit setup.

This internal entry point never replans, creates keys/accounts/schema, repairs
changed imported data, or rolls back/deletes files. CLI exposure still requires
runtime-key acceptance, rollback/cleanup and platform release gates.
"""

from dataclasses import dataclass
from pathlib import Path
import os

from .bundle import load_recovery_plan, _absolute
from .legacy_json import MigrationError, verify_backup
from .locking import migration_lock
from .publication import publish_files, verify_publication
from .sql_import import commit_sql_rows, recover_sql_receipt
from .staging import stage_files
from . import publication, staging


@dataclass(frozen=True)
class ImportReceipt:
    run_id: str
    status: str
    plan_digest: str
    data_digest: str
    rows: int
    files: int
    directories: int

    def report(self):
        return {
            "stage": "coordinated_sql_file_import",
            "run_id": self.run_id,
            "status": self.status,
            "plan_digest": self.plan_digest,
            "data_digest": self.data_digest,
            "verified_rows": self.rows,
            "verified_files": self.files,
            "verified_directories": self.directories,
            "target_runtime_configuration_verified": False,
            "rollback_available": False,
            "staging_and_backup_must_be_retained": True,
        }


def _needs_staging(recovery, lock):
    """Dispatch only; every selected branch authenticates its evidence again."""
    try:
        with staging._directory(lock.home_fd, staging._ROOT) as (root, _):
            with staging._directory(root, recovery.run_id) as (run, _):
                names = os.listdir(run)
                if any(
                    publication._RECORD.fullmatch(n) or publication._TEMP.fullmatch(n)
                    for n in names
                ):
                    return False  # Never recopy/reinitialize an interrupted publisher.
                ready = sum(
                    bool(staging._ATTEMPT.fullmatch(n)) and n.startswith("ready-")
                    for n in names
                )
                return ready != len(recovery.plan.files)
    except FileNotFoundError:
        return True


def apply_saved_plan(
    path: Path,
    *,
    database_url: str,
    target_home: Path,
    key: bytes,
    target_stopped: bool = False,
    max_incomplete_attempts: int = 8,
) -> ImportReceipt:
    """Explicit write/recovery: continue the SAME authenticated plan only.

    Committed SQL is verified with its published files, never repaired. An
    uncommitted attempt requires the independent backup online and verified;
    the original source may be offline. Connection errors never authorize cleanup.
    Caller must provision this exact key consistently for the target runtime.
    """
    if target_stopped is not True:
        raise MigrationError(
            "Coordinated import requires all target application writers stopped"
        )
    if (
        type(max_incomplete_attempts) is not int
        or not 1 <= max_incomplete_attempts <= 100
    ):
        raise MigrationError(
            "Incomplete staging attempt limit must be between 1 and 100"
        )
    selected = Path(target_home)
    _absolute(selected)
    if selected.is_symlink():
        raise MigrationError("Target home must not be a final symbolic link")
    try:
        # Common platform aliases (macOS /tmp -> /private/tmp) must bind to the
        # same canonical directory as the saved plan, never to a new location.
        target_home = selected.resolve(strict=True)
    except OSError:
        raise MigrationError("Target home must already exist") from None
    # Reject wrong target/key selection before even creating a lock file.
    initial = load_recovery_plan(
        path, database_url=database_url, target_home=target_home, key=key
    )
    try:
        with migration_lock(target_home, target_stopped=True) as lock:
            recovery = load_recovery_plan(
                path, database_url=database_url, target_home=target_home, key=key
            )
            if (recovery.run_id, recovery.digest) != (initial.run_id, initial.digest):
                raise MigrationError(
                    "Saved recovery plan changed before the lock was acquired"
                )
            # Explicit engine recovery is allowed here. The persisted target token
            # is checked before an absent receipt can select any file-writing path.
            sql = recover_sql_receipt(
                recovery.plan,
                database_url=database_url,
                run_id=recovery.run_id,
                target_anchor=recovery.target_anchor,
                target_stopped=True,
            )
            if sql is None:
                verify_backup(recovery.backup, recovery.plan.source)
                if _needs_staging(recovery, lock):
                    stage_files(
                        recovery,
                        lock=lock,
                        database_url=database_url,
                        key=key,
                        max_incomplete_attempts=max_incomplete_attempts,
                    )
                # Authenticate retained staging before even provisional SQL DML.
                with publication._prepared(recovery, lock, database_url, key):
                    pass
                lock.check()
                sql = commit_sql_rows(
                    recovery.plan,
                    database_url=database_url,
                    run_id=recovery.run_id,
                    target_anchor=recovery.target_anchor,
                    target_key=key,
                    target_stopped=True,
                    recover_sqlite=True,
                    before_commit=lambda: publish_files(
                        recovery, lock=lock, database_url=database_url, key=key
                    ),
                )
            # commit_sql_rows intentionally skips its hook on SQL replay. File
            # verification therefore MUST be independent of whether SQL was new.
            files = verify_publication(
                recovery, lock=lock, database_url=database_url, key=key
            )
            lock.check()
            return ImportReceipt(
                recovery.run_id,
                sql.status,
                sql.plan_digest,
                sql.data_digest,
                sql.row_count,
                files.files,
                files.directories,
            )
    except OSError:
        raise MigrationError(
            "Coordinated import could not finish; preserve the backup, staging and journal and retry only the same saved plan"
        ) from None
