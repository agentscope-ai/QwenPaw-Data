"""SQL component tests, not full file import/rollback acceptance."""

import json
from uuid import uuid4
from dataclasses import replace

import pytest
from sqlalchemy import create_engine, func, select, text
from sqlalchemy.engine import Connection
from sqlalchemy.exc import OperationalError, SQLAlchemyError

from qwenpaw_data.host.core.db.tables import Base
from qwenpaw_data.host.core.migration.legacy_json import MigrationError, scan_source
from qwenpaw_data.host.core.migration.sql_import import (
    commit_sql_rows,
    plan_digest,
    read_sql_receipt,
    recover_sql_receipt,
)
from qwenpaw_data.host.core.migration.target import validate_target

from .test_legacy_migration_plan import prepare, source as make_source
from .test_legacy_target_validation import initialize_target, mutate


KEY = bytes(range(32))


@pytest.fixture
def prepared(tmp_path):
    source = make_source.__wrapped__(tmp_path)
    plan = prepare(source)
    path = tmp_path / "target.sqlite"
    return plan, initialize_target(path), path


def counts(url):
    engine = create_engine(url)
    try:
        with engine.connect() as conn:
            return {
                name: conn.scalar(select(func.count()).select_from(table))
                for name, table in Base.metadata.tables.items()
            }
    finally:
        engine.dispose()


def run(plan, url, run_id, callback=lambda: None, **kwargs):
    return commit_sql_rows(
        plan,
        database_url=url,
        run_id=run_id,
        target_key=KEY,
        before_commit=callback,
        target_stopped=True,
        **kwargs,
    )


def test_rows_and_commit_receipt_are_atomic_and_exact_replay_is_a_noop(prepared):
    plan, url, path = prepared
    before_source = scan_source(plan.source.home).digest
    before = counts(url)
    run_id = uuid4().hex
    calls = []

    def file_hook():
        calls.append(True)
        # Another connection must not see staged data or a premature receipt.
        assert counts(url) == before

    receipt = run(plan, url, run_id, file_hook)
    assert receipt.status == "committed" and calls == [True]
    after = counts(url)
    assert after["legacy_import_runs"] == 1
    assert sum(after.values()) == len(plan.rows) + 1
    assert receipt.row_count == len(plan.rows) and receipt.file_count == 2
    assert receipt.report()["files_verified"] is False
    assert "synthetic-private-key" not in json.dumps(receipt.report()) + repr(receipt)
    read = read_sql_receipt(plan, database_url=url, run_id=run_id, target_stopped=True)
    assert read.status == "already_committed"
    assert read.data_digest == receipt.data_digest
    before_bytes = path.read_bytes()
    assert run(plan, url, run_id, file_hook).status == "already_committed"
    assert path.read_bytes() == before_bytes and calls == [True]
    assert (
        counts(url) == after and scan_source(plan.source.home).digest == before_source
    )
    assert not plan.target_home.exists()  # This component does not publish files.


def test_file_hook_failure_rolls_back_every_row_and_marker(prepared):
    plan, url, path = prepared
    before = counts(url)
    run_id = uuid4().hex

    def fail():
        raise RuntimeError("private callback contents")

    with pytest.raises(MigrationError, match="not committed") as raised:
        run(plan, url, run_id, fail)
    assert "private callback" not in str(raised.value)
    assert counts(url) == before
    assert (
        read_sql_receipt(plan, database_url=url, run_id=run_id, target_stopped=True)
        is None
    )
    assert run(plan, url, run_id).status == "committed"


def test_database_failure_mid_insert_rolls_back_before_file_hook(prepared, monkeypatch):
    plan, url, _ = prepared
    before = counts(url)
    original = Connection.execute
    called = []

    def failing_execute(self, statement, *args, **kwargs):
        if (
            getattr(statement, "is_insert", False)
            and statement.table.name == "chat_events"
        ):
            raise SQLAlchemyError("private driver payload")
        return original(self, statement, *args, **kwargs)

    monkeypatch.setattr(Connection, "execute", failing_execute)
    with pytest.raises(MigrationError) as raised:
        run(plan, url, uuid4().hex, lambda: called.append(True))
    assert "private driver" not in str(raised.value)
    assert not called and counts(url) == before


def test_commit_ack_loss_is_resolved_by_receipt_not_assumed_rollback(
    prepared, monkeypatch
):
    plan, url, _ = prepared
    run_id = uuid4().hex
    original = Connection.commit
    calls = []

    def commit_then_disconnect(self):
        original(self)
        raise OperationalError(
            "private statement", {}, RuntimeError("private driver secret")
        )

    with monkeypatch.context() as patch:
        patch.setattr(Connection, "commit", commit_then_disconnect)
        with pytest.raises(MigrationError, match="durable run receipt") as raised:
            run(plan, url, run_id, lambda: calls.append(True))
        assert "private driver secret" not in str(raised.value)
    receipt = read_sql_receipt(
        plan, database_url=url, run_id=run_id, target_stopped=True
    )
    assert receipt is not None and receipt.status == "already_committed"
    assert (
        run(plan, url, run_id, lambda: calls.append(True)).status == "already_committed"
    )
    assert calls == [True]


@pytest.mark.parametrize(
    "change",
    [
        "UPDATE sessions SET title='changed'",
        "UPDATE sessions SET share_scope='workspace'",
        "DELETE FROM chat_events WHERE sequence_number=0",
        "UPDATE legacy_import_runs SET version=2",
    ],
)
def test_modified_or_missing_imported_rows_and_receipts_block_replay(prepared, change):
    plan, url, _ = prepared
    run_id = uuid4().hex
    run(plan, url, run_id)
    mutate(url, change)
    before = counts(url)
    with pytest.raises(MigrationError):
        run(plan, url, run_id)
    assert counts(url) == before


def test_same_run_id_cannot_adopt_a_regenerated_or_changed_plan(prepared):
    plan, url, _ = prepared
    run_id = uuid4().hex
    run(plan, url, run_id)
    again = prepare(plan.source.home)
    assert plan_digest(again) != plan_digest(
        plan
    )  # Fresh encrypted tokens are distinct.
    with pytest.raises(MigrationError, match="different plan"):
        run(again, url, run_id)
    with pytest.raises(MigrationError, match="primary-key"):
        run(plan, url, uuid4().hex)


def test_authorization_is_rechecked_inside_transaction(prepared):
    plan, url, _ = prepared
    assert not validate_target(plan, database_url=url, target_stopped=True).issues
    mutate(url, "DELETE FROM role_bindings")
    with pytest.raises(MigrationError, match="authorization"):
        run(plan, url, uuid4().hex)
    assert not any(counts(url).values())


def test_missing_receipt_schema_is_not_created_by_import(prepared):
    plan, url, _ = prepared
    mutate(url, "DROP TABLE legacy_import_runs")
    with pytest.raises(MigrationError, match="schema"):
        run(plan, url, uuid4().hex)
    engine = create_engine(url)
    from sqlalchemy import inspect

    assert "legacy_import_runs" not in inspect(engine).get_table_names()
    engine.dispose()


def test_wrong_target_key_fails_before_any_sql_write(prepared):
    plan, url, _ = prepared
    with pytest.raises(MigrationError, match="authenticate"):
        commit_sql_rows(
            plan,
            database_url=url,
            run_id=uuid4().hex,
            target_key=bytes(range(32, 64)),
            before_commit=lambda: None,
            target_stopped=True,
        )
    assert not any(counts(url).values())


def test_source_plan_caller_mutations_do_not_change_the_transaction(prepared):
    plan, url, _ = prepared
    fingerprint = plan_digest(plan)

    def mutate_caller():
        plan.rows[0].values["title"] = "caller mutation"

    receipt = run(plan, url, uuid4().hex, mutate_caller)
    assert receipt.plan_digest == fingerprint != plan_digest(plan)
    engine = create_engine(url)
    with engine.connect() as conn:
        assert conn.scalar(text("SELECT title FROM sessions")) != "caller mutation"
    engine.dispose()


def test_receipt_lookup_does_not_treat_connectivity_failure_as_absence(
    prepared, tmp_path
):
    plan, _, _ = prepared
    absent = tmp_path / "absent.sqlite"
    with pytest.raises(MigrationError):
        read_sql_receipt(
            plan,
            database_url=f"sqlite:///{absent}",
            run_id=uuid4().hex,
            target_stopped=True,
        )
    assert not absent.exists()


def test_additive_runtime_schema_alignment_preserves_existing_data(prepared):
    from qwenpaw_data.host.core.db.align import align_host_schema

    _, url, _ = prepared
    mutate(url, "DROP TABLE legacy_import_runs")
    engine = create_engine(url)
    with engine.begin() as conn:
        users = list(conn.execute(text("SELECT * FROM users")))
        align_host_schema(conn)
        align_host_schema(conn)
        assert list(conn.execute(text("SELECT * FROM users"))) == users
        assert conn.scalar(text("SELECT count(*) FROM legacy_import_runs")) == 0
    engine.dispose()


def test_explicit_sqlite_recovery_uses_engine_without_manual_journal_deletion(prepared):
    plan, url, path = prepared
    journal = path.with_name(path.name + "-journal")
    journal.write_bytes(b"cold interrupted journal")
    run_id = uuid4().hex
    with pytest.raises(MigrationError, match="sidecars"):
        read_sql_receipt(plan, database_url=url, run_id=run_id, target_stopped=True)
    with pytest.raises(MigrationError, match="sidecars"):
        run(plan, url, run_id)
    assert (
        recover_sql_receipt(plan, database_url=url, run_id=run_id, target_stopped=True)
        is None
    )
    assert not any(counts(url).values())
    assert run(plan, url, run_id, recover_sqlite=True).status == "committed"
    assert (
        recover_sql_receipt(
            plan, database_url=url, run_id=run_id, target_stopped=True
        ).status
        == "already_committed"
    )


@pytest.mark.parametrize("suffix", ["-wal", "-shm", "-journal"])
def test_sqlite_recovery_refuses_symlinked_sidecars(prepared, tmp_path, suffix):
    plan, url, path = prepared
    other = tmp_path / "unrelated"
    other.write_bytes(b"preserve this file")
    path.with_name(path.name + suffix).symlink_to(other)
    before = path.read_bytes()
    with pytest.raises(MigrationError, match="sidecars"):
        recover_sql_receipt(
            plan, database_url=url, run_id=uuid4().hex, target_stopped=True
        )
    assert other.read_bytes() == b"preserve this file"
    assert path.read_bytes() == before


def test_large_import_uses_bounded_insert_batches_and_preserves_exact_replay(
    prepared, monkeypatch
):
    plan, url, _ = prepared
    session = next(row for row in plan.rows if row.table == "sessions")
    records = tuple(
        replace(session, values={**session.values, "id": f"session-{n}"})
        for n in range(1001)
    )
    plan = replace(plan, rows=records, files=(), directories=())
    original = Connection.execute
    sizes = []

    def tracked(self, statement, *args, **kwargs):
        if (
            getattr(statement, "is_insert", False)
            and statement.table.name == "sessions"
        ):
            sizes.append(len(args[0]))
        return original(self, statement, *args, **kwargs)

    monkeypatch.setattr(Connection, "execute", tracked)
    run_id = uuid4().hex
    receipt = run(plan, url, run_id)
    assert receipt.row_count == 1001
    assert sum(sizes) == 1001 and max(sizes) == 200 and len(sizes) == 6
    assert run(plan, url, run_id).data_digest == receipt.data_digest
    assert len(sizes) == 6
