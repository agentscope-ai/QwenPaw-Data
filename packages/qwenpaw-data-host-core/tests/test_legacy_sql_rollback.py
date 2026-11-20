"""SQL rollback/tombstone checks; no production file cleanup is invoked here."""

from dataclasses import replace
import os
import subprocess
import sys
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Connection
from sqlalchemy.exc import OperationalError, SQLAlchemyError

from qwenpaw_data.host.core.migration.legacy_json import MigrationError
from qwenpaw_data.host.core.migration.locking import migration_lock
from qwenpaw_data.host.core.migration.publication import verify_publication
from qwenpaw_data.host.core.migration.sql_import import (
    commit_sql_rows,
    read_sql_receipt,
    recover_sql_receipt,
)
from qwenpaw_data.host.core.migration.sql_rollback import (
    rollback_sql_rows,
    read_sql_rollback_receipt,
)

from .test_legacy_recovery_bundle import KEY, context as bundle_context, load, save
from .test_legacy_import_coordinator import apply
from .test_legacy_sql_import import counts
from .test_legacy_rollback_check import add
from .test_legacy_target_validation import mutate


pytestmark = pytest.mark.skipif(
    os.name != "posix", reason="POSIX saved-plan/file fixtures"
)


@pytest.fixture
def context(tmp_path):
    result = bundle_context.__wrapped__(tmp_path)
    save(result)
    apply(result)
    return result


def read(context, **kwargs):
    return read_sql_rollback_receipt(
        context[0],
        database_url=context[1],
        run_id=context[4],
        target_anchor=context[2],
        target_stopped=True,
        **kwargs,
    )


def rollback(context, callback=lambda: None, **kwargs):
    return rollback_sql_rows(
        context[0],
        database_url=context[1],
        run_id=context[4],
        target_anchor=context[2],
        target_stopped=True,
        before_commit=callback,
        **kwargs,
    )


def test_delete_and_tombstone_are_atomic_and_all_files_remain(context):
    before = counts(context[1])
    files = [context[0].target_home / f.destination for f in context[0].files]
    original = [(p.read_bytes(), p.stat().st_ino, p.stat().st_nlink) for p in files]
    recovery = load(context)
    called = []
    with migration_lock(recovery.plan.target_home, target_stopped=True) as lock:

        def hook():
            assert counts(context[1]) == before  # Uncommitted deletion is invisible.
            verify_publication(recovery, lock=lock, database_url=context[1], key=KEY)
            called.append(True)

        result = rollback(context, hook)
    assert result.status == "rolled_back" and called == [True]
    assert not result.report()["files_removed"]
    after = counts(context[1])
    for name in {r.table for r in context[0].rows}:
        assert after[name] == 0
    assert (
        after["legacy_import_runs"]
        == after["legacy_import_rollbacks"]
        == after["legacy_import_targets"]
        == 1
    )
    assert read(context).data_digest == result.data_digest
    assert rollback(context, hook).status == "already_rolled_back"
    assert called == [True] and counts(context[1]) == after
    assert [
        (p.read_bytes(), p.stat().st_ino, p.stat().st_nlink) for p in files
    ] == original
    for action in (
        lambda: apply(context),
        lambda: read_sql_receipt(
            context[0], database_url=context[1], run_id=context[4], target_stopped=True
        ),
        lambda: recover_sql_receipt(
            context[0], database_url=context[1], run_id=context[4], target_stopped=True
        ),
        lambda: commit_sql_rows(
            context[0],
            database_url=context[1],
            run_id=context[4],
            target_key=KEY,
            target_stopped=True,
            before_commit=lambda: None,
        ),
    ):
        with pytest.raises(MigrationError, match="forward replay is forbidden"):
            action()
    assert counts(context[1]) == after


def test_failure_mid_delete_restores_all_rows_and_does_not_call_file_hook(
    context, monkeypatch
):
    before = counts(context[1])
    original = Connection.execute
    calls = []

    def fail(self, statement, *args, **kwargs):
        if (
            getattr(statement, "is_delete", False)
            and statement.table.name == "chat_events"
        ):
            raise SQLAlchemyError("private driver secret")
        return original(self, statement, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(Connection, "execute", fail)
        with pytest.raises(MigrationError, match="rollback receipt") as raised:
            rollback(context, lambda: calls.append(True))
    assert "private driver secret" not in str(raised.value)
    assert counts(context[1]) == before and not calls and read(context) is None


def test_file_hook_error_rolls_back_deletion_without_permission_to_cleanup(context):
    before = counts(context[1])

    def fail():
        raise RuntimeError("private callback contents")

    with pytest.raises(MigrationError, match="not committed") as raised:
        rollback(context, fail)
    assert "private callback contents" not in str(raised.value)
    assert counts(context[1]) == before and read(context) is None
    assert apply(context).status == "already_committed"


def test_lost_rollback_commit_response_is_resolved_by_tombstone(context, monkeypatch):
    original = Connection.commit

    def lose_response(self):
        original(self)
        raise OperationalError("private statement", {}, RuntimeError("private secret"))

    with monkeypatch.context() as patch:
        patch.setattr(Connection, "commit", lose_response)
        with pytest.raises(MigrationError, match="durable rollback receipt") as raised:
            rollback(context)
    assert "private secret" not in str(raised.value)
    assert read(context).status == "already_rolled_back"
    assert rollback(context).status == "already_rolled_back"
    assert all(
        (context[0].target_home / f.destination).exists() for f in context[0].files
    )


@pytest.mark.parametrize("change", ["row", "reference", "trigger", "token"])
def test_revalidation_inside_delete_transaction_refuses_changed_state(context, change):
    if change == "row":
        mutate(context[1], "UPDATE sessions SET title='new work'")
    elif change == "reference":
        add(
            context,
            "behavior_events",
            dict(id="new-event", session_id="s1", kind="read"),
        )
    elif change == "trigger":
        mutate(
            context[1],
            "CREATE TRIGGER preserve AFTER DELETE ON chats BEGIN SELECT 1; END",
        )
    else:
        mutate(context[1], "DELETE FROM legacy_import_targets")
    before = counts(context[1])
    called = []
    with pytest.raises(MigrationError):
        rollback(context, lambda: called.append(True))
    assert counts(context[1]) == before and not called


@pytest.mark.parametrize(
    "change", ["new_reference", "reused_pk", "original_receipt", "rollback_receipt"]
)
def test_existing_rollback_receipt_is_not_blind_cleanup_authority(context, change):
    rollback(context)
    if change == "new_reference":
        add(
            context, "chat_commands", dict(id="new-command", chat_id="c1", kind="steer")
        )
    elif change == "reused_pk":
        add(context, "sessions", dict(id="s1", title="new user data"))
    elif change == "original_receipt":
        mutate(context[1], "DELETE FROM legacy_import_runs")
    else:
        mutate(context[1], "UPDATE legacy_import_rollbacks SET data_digest='changed'")
    before = counts(context[1])
    with pytest.raises(MigrationError):
        read(context)
    with pytest.raises(MigrationError):
        rollback(context)
    assert counts(context[1]) == before


def test_unrelated_data_and_auth_are_never_deleted(context):
    add(
        context,
        "sessions",
        dict(id="unrelated", user_id="another-user", title="preserve"),
    )
    result = rollback(context)
    assert result.row_count == len(context[0].rows)
    engine = create_engine(context[1])
    with engine.connect() as conn:
        assert conn.execute(text("SELECT id,title FROM sessions")).all() == [
            ("unrelated", "preserve")
        ]
        assert conn.scalar(text("SELECT count(*) FROM users")) == 1
    engine.dispose()


def test_missing_rollback_schema_never_means_no_rollback(context):
    mutate(context[1], "DROP TABLE legacy_import_rollbacks")
    before = counts_without_rollback(context[1])
    for action in (
        lambda: rollback(context),
        lambda: read(context),
        lambda: apply(context),
    ):
        with pytest.raises(MigrationError, match="legacy_import_rollbacks"):
            action()
    assert counts_without_rollback(context[1]) == before


def counts_without_rollback(url):
    engine = create_engine(url)
    with engine.connect() as conn:
        result = conn.scalar(text("SELECT count(*) FROM chats"))
    engine.dispose()
    return result


def test_large_sql_rollback_batches_exact_composite_keys(tmp_path):
    result = bundle_context.__wrapped__(tmp_path)
    original = next(row for row in result[0].rows if row.table == "feedbacks")
    extra = tuple(
        replace(original, values={**original.values, "id": f"bulk-{n}"})
        for n in range(1001)
    )
    plan = replace(result[0], rows=(*result[0].rows, *extra))
    run_id = uuid4().hex
    commit_sql_rows(
        plan,
        database_url=result[1],
        run_id=run_id,
        target_key=KEY,
        target_anchor=result[2],
        target_stopped=True,
        before_commit=lambda: None,
    )
    receipt = rollback_sql_rows(
        plan,
        database_url=result[1],
        run_id=run_id,
        target_anchor=result[2],
        target_stopped=True,
        before_commit=lambda: None,
    )
    assert receipt.row_count == len(plan.rows)
    assert counts(result[1])["feedbacks"] == 0


def test_rollback_requires_stopped_writers_and_file_hook(context):
    with pytest.raises(MigrationError, match="explicit file-journal hook"):
        rollback_sql_rows(
            context[0],
            database_url=context[1],
            run_id=context[4],
            target_anchor=context[2],
            before_commit=lambda: None,
        )
    with pytest.raises(MigrationError, match="explicit file-journal hook"):
        rollback_sql_rows(
            context[0],
            database_url=context[1],
            run_id=context[4],
            target_anchor=context[2],
            target_stopped=True,
            before_commit=None,
        )


_WORKER = """
import os, sys
from pathlib import Path
from qwenpaw_data.host.core.migration.bundle import load_recovery_plan
from qwenpaw_data.host.core.migration.locking import migration_lock
from qwenpaw_data.host.core.migration.publication import verify_publication
from qwenpaw_data.host.core.migration.sql_rollback import rollback_sql_rows, read_sql_rollback_receipt
path, home, url, action = sys.argv[1:]
key = bytes(range(32))
recovery = load_recovery_plan(Path(path), database_url=url, target_home=Path(home), key=key)
with migration_lock(recovery.plan.target_home, target_stopped=True) as lock:
    if action == 'receipt':
        value = read_sql_rollback_receipt(recovery.plan, database_url=url, run_id=recovery.run_id,
                target_anchor=recovery.target_anchor, target_stopped=True, recover_sqlite=True)
        print('absent' if value is None else value.status, flush=True)
    else:
        def hook():
            verify_publication(recovery, lock=lock, database_url=url, key=key)
            if action == 'crash-before-commit':
                os._exit(81)
        value = rollback_sql_rows(recovery.plan, database_url=url, run_id=recovery.run_id,
                 target_anchor=recovery.target_anchor, target_stopped=True,
                 recover_sqlite=True, before_commit=hook)
        if action == 'lost-response':
            os._exit(82)
        print(value.status, flush=True)
"""


@pytest.mark.parametrize(
    "action,code", [("crash-before-commit", 81), ("lost-response", 82)]
)
def test_fresh_process_rollback_crash_or_response_loss_preserves_file_evidence(
    context, action, code
):
    args = [
        sys.executable,
        "-c",
        _WORKER,
        str(context[3]),
        str(context[0].target_home),
        context[1],
    ]
    before = counts(context[1])
    files = [context[0].target_home / f.destination for f in context[0].files]
    content = [(f.read_bytes(), f.stat().st_ino) for f in files]

    def child(mode, expected=0):
        result = subprocess.run(
            [*args, mode], text=True, capture_output=True, timeout=30
        )
        assert result.returncode == expected, result.stderr
        return result.stdout.strip()

    child(action, code)
    committed = action == "lost-response"
    assert child("receipt") == ("already_rolled_back" if committed else "absent")
    if not committed:
        assert counts(context[1]) == before
    assert child("retry") == ("already_rolled_back" if committed else "rolled_back")
    assert child("retry") == "already_rolled_back"
    assert [(f.read_bytes(), f.stat().st_ino) for f in files] == content
    with pytest.raises(MigrationError, match="forward replay is forbidden"):
        apply(context)
