"""Read-only rollback eligibility, reusable inside a later delete transaction.

No delete/repair is performed. Closed native schema and bounded reference scans
are intentional: unknown extensions or ambiguous JSON references require review.
External systems and arbitrary references embedded in prose/code are not proven.
"""

from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import Text, cast, func, inspect, select, text
from sqlalchemy.types import JSON

from qwenpaw_data.host.core.db.tables import Base
from qwenpaw_data_auth.database import metadata as auth_metadata

from .bundle import load_recovery_plan, _absolute
from .legacy_json import MigrationError, _json, verify_backup
from .locking import migration_lock
from .publication import verify_publication
from .sql_import import _receipt, _run_id, _TABLES, plan_digest
from .binding import assert_target_anchor
from .target import _check_schema, _read_only_target


@dataclass(frozen=True, order=True)
class RollbackFinding:
    code: str
    table: str
    field: str = ""


@dataclass(frozen=True)
class RollbackCheck:
    run_id: str
    plan_digest: str
    data_digest: str
    rows: int
    files: int
    scanned_rows: int
    findings: tuple[RollbackFinding, ...]

    def report(self):
        return {
            "stage": "rollback_eligibility_only",
            "run_id": self.run_id,
            "plan_digest": self.plan_digest,
            "data_digest": self.data_digest,
            "imported_rows": self.rows,
            "imported_files": self.files,
            "scanned_rows": self.scanned_rows,
            "native_dependencies_clear": not self.findings,
            "findings": [vars(f) for f in self.findings],
            "sql_written": False,
            "files_deleted": False,
            "rollback_available": False,
            "external_dependencies_checked": False,
        }


def _schema(conn):
    inspector = inspect(conn)
    native = set(Base.metadata.tables)
    _check_schema(conn, native)
    known = native | set(auth_metadata.tables)
    present = set(inspector.get_table_names())
    if present - known:
        raise MigrationError("Rollback requires review of unrecognized database tables")
    for name in native:
        if {c["name"] for c in inspector.get_columns(name)} != set(
            Base.metadata.tables[name].columns.keys()
        ):
            raise MigrationError(
                "Rollback requires review of additional native-table columns"
            )
    for name in present & set(auth_metadata.tables):
        if {c["name"] for c in inspector.get_columns(name)} != set(
            auth_metadata.tables[name].columns.keys()
        ):
            raise MigrationError(
                "Rollback requires review of additional Auth-table columns"
            )
    # Native repositories model these relationships without database FKs today.
    # Added constraints/cascades must not silently broaden a future DELETE.
    if any(
        fk.get("referred_table") in _TABLES
        for name in present
        for fk in inspector.get_foreign_keys(name)
    ):
        raise MigrationError(
            "Rollback requires review of foreign keys into imported tables"
        )
    if conn.dialect.name == "sqlite":
        triggers = conn.execute(
            text("SELECT tbl_name FROM sqlite_master WHERE type='trigger'")
        ).scalars()
    elif conn.dialect.name == "postgresql":
        triggers = conn.execute(
            text(
                "SELECT c.relname FROM pg_trigger t JOIN pg_class c ON c.oid=t.tgrelid JOIN pg_namespace n ON n.oid=c.relnamespace WHERE NOT t.tgisinternal AND n.nspname=current_schema()"
            )
        ).scalars()
    else:
        raise MigrationError(
            "Rollback dependency checks support SQLite or PostgreSQL only"
        )
    if any(name in native for name in triggers):
        raise MigrationError("Rollback requires review of native-table triggers")


def _json_reference(value, tokens, providers, models, user_id):
    pending = [value]
    while pending:
        item = pending.pop()
        if isinstance(item, str) and item in tokens:
            return True
        if isinstance(item, dict):
            provider, model = item.get("provider_id"), item.get("model_id")
            if isinstance(provider, str) and (
                (user_id, provider) in providers
                or (isinstance(model, str) and (user_id, provider, model) in models)
            ):
                return True
            pending.extend(item.keys())
            pending.extend(item.values())
        elif isinstance(item, list):
            pending.extend(item)
    return False


def check_rollback_dependencies(
    conn,
    plan,
    *,
    run_id,
    target_anchor,
    max_rows=100_000,
    max_json_bytes=1024**2,
    max_total_json_bytes=64 * 1024**2,
) -> RollbackCheck:
    """Validate exact imported rows plus native references; no filesystem or DML.

    This must be repeated IN the eventual SQL deletion transaction. A successful
    earlier inspection is never permission to delete without revalidation.
    """
    if any(
        type(v) is not int or v <= 0
        for v in (max_rows, max_json_bytes, max_total_json_bytes)
    ):
        raise MigrationError("Rollback scan limits must be positive integers")
    _run_id(run_id)
    assert_target_anchor(conn, target_anchor)
    _schema(conn)
    digest = plan_digest(plan)
    receipt = _receipt(conn, plan, run_id, digest)
    if receipt is None:
        raise MigrationError("No verified committed import exists for rollback")
    imported = {}
    ids = {}
    providers, models = set(), set()
    for row in plan.rows:
        table = Base.metadata.tables[row.table]
        imported.setdefault(row.table, set()).add(
            tuple(row.values[c.name] for c in table.primary_key.columns)
        )
        if row.table in {
            "sessions",
            "chats",
            "artifacts",
            "cron_jobs",
            "settlement_cards",
            "feedbacks",
        }:
            ids.setdefault(row.table, set()).add(row.values["id"])
        if row.table == "attachments":
            ids.setdefault(row.table, set()).add(row.values["attachment_id"])
        if row.table == "user_providers":
            providers.add((row.values["user_id"], row.values["provider_id"]))
        if row.table == "user_provider_models":
            models.add(
                (
                    row.values["user_id"],
                    row.values["provider_id"],
                    row.values["model_id"],
                )
            )
    sessions, chats = ids.get("sessions", set()), ids.get("chats", set())
    tokens = set().union(*ids.values()) if ids else set()
    tokens.update(f.destination for f in plan.files)
    tokens.update(f.source.removeprefix("host/workspace/") for f in plan.files)
    references = {
        "session_id": sessions,
        "parent_session_id": sessions,
        "active_session_id": sessions,
        "chat_id": chats,
        "source_chat_id": chats,
        "forked_from_chat_id": chats,
        "attachment_id": ids.get("attachments", set()),
        "artifact_id": ids.get("artifacts", set()),
        "source_ref": tokens,
        "target_id": tokens,
    }
    findings = set()
    count = total_json = 0
    for name, table in sorted(Base.metadata.tables.items()):
        if name.startswith("legacy_import_"):
            continue
        keys = tuple(table.primary_key.columns)
        direct = [c for c in references if c in table.c]
        json_columns = [c for c in table.c if isinstance(c.type, JSON)]
        model_columns = [
            c
            for c in (
                "user_id",
                "provider_id",
                "model_id",
                "default_provider_id",
                "default_model_id",
                "light_provider_id",
                "light_model_id",
            )
            if c in table.c
        ]
        columns = list(
            {
                c.name: c
                for c in [*keys, *(table.c[n] for n in direct + model_columns)]
            }.values()
        )
        # Cast+substring bounds each JSON value before transferring it; streaming
        # and total row/byte budgets also cover unrelated users in this database.
        columns += [
            func.substr(cast(c, Text), 1, max_json_bytes + 1).label(
                f"_rollback_json_{c.name}"
            )
            for c in json_columns
        ]
        with conn.execute(
            select(*columns).limit(max_rows + 1).execution_options(yield_per=100)
        ) as result:
            for row in result.mappings():
                count += 1
                if count > max_rows:
                    raise MigrationError(
                        "Rollback dependency scan exceeds its row limit"
                    )
                if tuple(row[c.name] for c in keys) in imported.get(name, set()):
                    continue
                for column in direct:
                    if row[column] in references[column]:
                        findings.add(
                            RollbackFinding("new_native_reference", name, column)
                        )
                user = row.get("user_id")
                for prefix in ("", "default_", "light_"):
                    provider = row.get(prefix + "provider_id")
                    model = row.get(prefix + "model_id")
                    if (user, provider) in providers or (
                        user,
                        provider,
                        model,
                    ) in models:
                        findings.add(
                            RollbackFinding(
                                "remaining_model_dependency",
                                name,
                                prefix + "provider_id",
                            )
                        )
                for column in json_columns:
                    value = row[f"_rollback_json_{column.name}"]
                    if value is None:
                        continue
                    if (
                        not isinstance(value, str)
                        or len(value.encode("utf-8")) > max_json_bytes
                    ):
                        raise MigrationError(
                            "Rollback JSON value exceeds its byte limit"
                        )
                    total_json += len(value.encode("utf-8"))
                    if total_json > max_total_json_bytes:
                        raise MigrationError(
                            "Rollback dependency scan exceeds its total JSON limit"
                        )
                    if _json_reference(
                        _json(value.encode(), "rollback reference"),
                        tokens,
                        providers,
                        models,
                        user,
                    ):
                        findings.add(
                            RollbackFinding(
                                "possible_json_reference", name, column.name
                            )
                        )
    return RollbackCheck(
        run_id,
        digest,
        receipt.data_digest,
        receipt.row_count,
        receipt.file_count,
        count,
        tuple(sorted(findings)),
    )


def inspect_saved_rollback(
    path: Path,
    *,
    database_url: str,
    target_home: Path,
    key: bytes,
    target_stopped: bool = False,
    **limits,
):
    """No business/file deletion; a persistent lock file may be acquired/created."""
    if target_stopped is not True:
        raise MigrationError("Rollback inspection requires stopped target writers")
    selected = Path(target_home)
    _absolute(selected)
    if selected.is_symlink():
        raise MigrationError("Target home must not be a final symbolic link")
    try:
        target_home = selected.resolve(strict=True)
        recovery = load_recovery_plan(
            path, database_url=database_url, target_home=target_home, key=key
        )
        with migration_lock(target_home, target_stopped=True) as lock:
            verify_backup(recovery.backup, recovery.plan.source)
            with _read_only_target(database_url) as conn:
                result = check_rollback_dependencies(
                    conn,
                    recovery.plan,
                    run_id=recovery.run_id,
                    target_anchor=recovery.target_anchor,
                    **limits,
                )
            verify_publication(recovery, lock=lock, database_url=database_url, key=key)
            return result
    except OSError:
        raise MigrationError(
            "Cannot verify rollback eligibility; no deletion was attempted"
        ) from None
