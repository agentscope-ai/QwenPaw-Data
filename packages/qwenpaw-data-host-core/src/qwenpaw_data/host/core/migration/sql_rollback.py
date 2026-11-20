"""Atomic deletion of unchanged imported SQL rows, with a permanent tombstone.

Internal component only. A coordinator must retain the encrypted plan, verified
backup and owned file journal, and hold its home lock. SQL success does not prove
file rollback. Never clean up files based on a missing or unreadable receipt.
"""

from collections import defaultdict
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable

from sqlalchemy import and_, delete, insert, or_, select

from qwenpaw_data.host.core.db.tables import (
    Base,
    LegacyImportRollbackRow,
    LegacyImportRunRow,
)

from .binding import assert_target_anchor
from .legacy_json import MigrationError
from .plan import MigrationPlan
from .rollback_check import (
    _schema,
    _scan_remaining_references,
    check_rollback_dependencies,
)
from .sql_import import _run_id, _writer, plan_digest
from .target import _check_schema, _read_only_target


_ROLLBACK = LegacyImportRollbackRow.__table__
_RUN = LegacyImportRunRow.__table__


@dataclass(frozen=True)
class SQLRollbackReceipt:
    run_id: str
    plan_digest: str
    data_digest: str
    row_count: int
    file_count: int
    status: str

    def report(self):
        return {
            **vars(self),
            "stage": "sql_rollback_component",
            "sql_rows_deleted": self.status == "rolled_back",
            "files_removed": False,
            "forward_replay_forbidden": True,
        }


def _batches(plan):
    groups = defaultdict(list)
    for row in reversed(plan.rows):
        groups[row.table].append(row)
    for name, records in groups.items():
        table = Base.metadata.tables[name]
        keys = tuple(table.primary_key.columns)
        size = max(1, 400 // len(keys))
        for offset in range(0, len(records), size):
            selected = records[offset : offset + size]
            yield (
                table,
                or_(
                    *(
                        and_(*(key == row.values[key.name] for key in keys))
                        for row in selected
                    )
                ),
                len(selected),
            )


def _absent(conn, plan):
    for table, condition, _ in _batches(plan):
        if (
            conn.execute(
                select(*table.primary_key.columns).where(condition).limit(1)
            ).first()
            is not None
        ):
            raise MigrationError(
                "Rolled-back primary keys exist again; automatic cleanup or replay is refused"
            )


def _rollback_receipt(conn, plan, run_id, fingerprint, **limits):
    _check_schema(conn, {_ROLLBACK.name, _RUN.name} | {r.table for r in plan.rows})
    value = (
        conn.execute(select(_ROLLBACK).where(_ROLLBACK.c.id == run_id))
        .mappings()
        .one_or_none()
    )
    if value is None:
        return None
    original = (
        conn.execute(select(_RUN).where(_RUN.c.id == run_id)).mappings().one_or_none()
    )
    if (
        original is None
        or value["version"] != 1
        or original["version"] != 1
        or value["source_digest"] != plan.source.digest
        or value["plan_digest"] != fingerprint
        or value["row_count"] != len(plan.rows)
        or value["file_count"] != len(plan.files)
        or any(
            value[k] != original[k]
            for k in (
                "source_digest",
                "plan_digest",
                "data_digest",
                "row_count",
                "file_count",
            )
        )
    ):
        raise MigrationError(
            "Rollback receipt does not match the original committed import"
        )
    _schema(conn)
    _absent(conn, plan)
    if _scan_remaining_references(conn, plan, **limits)[1]:
        raise MigrationError(
            "New references appeared after SQL rollback; automatic file cleanup is refused"
        )
    return SQLRollbackReceipt(
        run_id,
        fingerprint,
        value["data_digest"],
        value["row_count"],
        value["file_count"],
        "already_rolled_back",
    )


def read_sql_rollback_receipt(
    plan: MigrationPlan,
    *,
    database_url: str,
    run_id: str,
    target_anchor: str,
    target_stopped: bool = False,
    recover_sqlite: bool = False,
    **limits,
) -> SQLRollbackReceipt | None:
    """None is only a successful bound read with no receipt; it never permits cleanup."""
    if target_stopped is not True or type(recover_sqlite) is not bool:
        raise MigrationError(
            "Rollback receipt inspection requires stopped writers and an explicit recovery boolean"
        )
    _run_id(run_id)
    fingerprint = plan_digest(plan)
    connection = (
        _writer(database_url, recover_sqlite=True, operation="rollback")
        if recover_sqlite
        else _read_only_target(database_url)
    )
    with connection as conn:
        assert_target_anchor(conn, target_anchor)
        return _rollback_receipt(conn, plan, run_id, fingerprint, **limits)


def rollback_sql_rows(
    plan: MigrationPlan,
    *,
    database_url: str,
    run_id: str,
    target_anchor: str,
    before_commit: Callable[[], None],
    target_stopped: bool = False,
    recover_sqlite: bool = False,
    **limits,
) -> SQLRollbackReceipt:
    """Delete exact imported PKs and commit the tombstone in the SAME transaction.

    before_commit MUST verify unchanged owned file publication and persist the
    coordinator's private rollback intent. It must not remove any files: a later
    commit error is ambiguous. Existing receipt replay skips this hook and only
    verifies absence/dependencies; the file coordinator owns subsequent recovery.
    """
    if (
        target_stopped is not True
        or not callable(before_commit)
        or type(recover_sqlite) is not bool
    ):
        raise MigrationError(
            "SQL rollback requires stopped writers and an explicit file-journal hook"
        )
    _run_id(run_id)
    plan = deepcopy(plan)
    fingerprint = plan_digest(plan)
    with _writer(
        database_url, recover_sqlite=recover_sqlite, operation="rollback"
    ) as conn:
        assert_target_anchor(conn, target_anchor)
        existing = _rollback_receipt(conn, plan, run_id, fingerprint, **limits)
        if existing is not None:
            return existing
        checked = check_rollback_dependencies(
            conn, plan, run_id=run_id, target_anchor=target_anchor, **limits
        )
        if checked.findings:
            raise MigrationError(
                "Remaining native or possible JSON dependencies forbid automatic rollback"
            )
        for table, condition, expected in _batches(plan):
            result = conn.execute(delete(table).where(condition))
            if result.rowcount != expected:
                raise MigrationError(
                    "SQL rollback did not delete exactly the recorded primary keys"
                )
        try:
            before_commit()
        except Exception:
            raise MigrationError(
                "Rollback file-journal hook failed; SQL deletion was not committed and no file cleanup is permitted"
            ) from None
        _absent(conn, plan)
        if _scan_remaining_references(conn, plan, **limits)[1]:
            raise MigrationError(
                "Dependencies changed before rollback commit; deletion was not committed"
            )
        conn.execute(
            insert(_ROLLBACK),
            dict(
                id=run_id,
                version=1,
                source_digest=plan.source.digest,
                plan_digest=fingerprint,
                data_digest=checked.data_digest,
                row_count=len(plan.rows),
                file_count=len(plan.files),
                rolled_back_at=datetime.now(timezone.utc),
            ),
        )
        conn.commit()
    return SQLRollbackReceipt(
        run_id,
        fingerprint,
        checked.data_digest,
        len(plan.rows),
        len(plan.files),
        "rolled_back",
    )
