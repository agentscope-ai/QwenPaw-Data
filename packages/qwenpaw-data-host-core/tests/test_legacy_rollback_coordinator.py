"""Owned file unlink requires durable SQL and authenticated local evidence."""

import json
import os
import subprocess
import sys

import pytest
from sqlalchemy import create_engine, select

from qwenpaw_data_auth.database import metadata

from qwenpaw_data.host.core.migration import rollback as module
from qwenpaw_data.host.core.migration.legacy_json import MigrationError

from .test_legacy_recovery_bundle import KEY, context as bundle_context, save
from .test_legacy_import_coordinator import apply
from .test_legacy_sql_import import counts
from .test_legacy_sql_rollback import rollback as sql_rollback
from .test_legacy_rollback_check import add
from .test_legacy_target_validation import mutate


pytestmark = pytest.mark.skipif(os.name != "posix", reason="POSIX file rollback")


@pytest.fixture
def context(tmp_path):
    result = bundle_context.__wrapped__(tmp_path)
    save(result)
    apply(result)
    return result


def rollback(context, **kwargs):
    options = dict(
        database_url=context[1],
        target_home=context[0].target_home,
        key=KEY,
        target_stopped=True,
    )
    options.update(kwargs)
    return module.rollback_saved_plan(context[3], **options)


def finals(context):
    return [context[0].target_home / f.destination for f in context[0].files]


def journal(context):
    return context[0].target_home / ".legacy-migrations" / context[4]


def auth_rows(context):
    engine = create_engine(context[1])
    try:
        with engine.connect() as conn:
            return {
                name: conn.execute(select(table)).all()
                for name, table in metadata.tables.items()
            }
    finally:
        engine.dispose()


def test_complete_rollback_preserves_backup_staging_auth_and_unrelated_files(context):
    plan = context[0]
    auth = auth_rows(context)
    unrelated = finals(context)[0].with_name("user-owned.txt")
    unrelated.write_bytes(b"new unrelated work")
    plan.source.home.rename(plan.source.home.with_name("source-offline"))
    original = [(p.read_bytes(), p.stat().st_ino) for p in finals(context)]
    receipt = rollback(context)
    assert receipt.status == "rolled_back" and receipt.files == 2
    assert all(not p.exists() for p in finals(context))
    retained = list(journal(context).glob("ready-*/data"))
    assert sorted((p.read_bytes(), p.stat().st_ino) for p in retained) == sorted(
        original
    )
    assert all(p.stat().st_nlink == 1 for p in retained)
    assert all((plan.target_home / p).is_dir() for p in plan.directories)
    assert (plan.source.home.parent / "backup").is_dir()
    assert unrelated.read_bytes() == b"new unrelated work"
    after = counts(context[1])
    assert all(after[t] == 0 for t in {r.table for r in plan.rows})
    assert after["legacy_import_runs"] == after["legacy_import_rollbacks"] == 1
    assert auth_rows(context) == auth
    assert rollback(context).status == "already_rolled_back"
    with pytest.raises(MigrationError, match="forward replay is forbidden"):
        apply(context)


@pytest.mark.parametrize(
    "change", ["bytes", "rename", "replace", "symlink", "backup", "rows"]
)
def test_changed_data_refuses_before_sql_or_file_deletion(context, change):
    final = finals(context)[0]
    if change == "bytes":
        final.write_bytes(b"new user bytes")
    elif change == "rename":
        final.rename(final.with_name("user-moved"))
    elif change == "replace":
        data = final.read_bytes()
        final.unlink()
        final.write_bytes(data)
        final.chmod(0o600)
    elif change == "symlink":
        final.unlink()
        final.symlink_to(next(journal(context).glob("ready-*/data")))
    elif change == "backup":
        backup = context[0].source.home.parent / "backup"
        backup.rename(backup.with_name("backup-offline"))
    else:
        mutate(context[1], "UPDATE sessions SET title='new work'")
    before = counts(context[1])
    with pytest.raises(MigrationError):
        rollback(context)
    assert counts(context[1]) == before
    assert finals(context)[1].exists()
    assert not (journal(context) / "rollback-intent.enc").exists()


def test_sql_tombstone_without_local_intent_never_authorizes_unlink(context):
    sql_rollback(context)
    with pytest.raises(MigrationError, match="no authenticated local rollback intent"):
        rollback(context)
    assert all(p.exists() for p in finals(context))


def test_ambiguous_commit_retains_files_and_retry_uses_receipt(context, monkeypatch):
    original = module.rollback_sql_rows

    def lost(*args, **kwargs):
        original(*args, **kwargs)
        raise OSError("lost SQL response")

    with monkeypatch.context() as patch:
        patch.setattr(module, "rollback_sql_rows", lost)
        with pytest.raises(MigrationError, match="retain backup"):
            rollback(context)
    assert all(p.exists() for p in finals(context))
    assert counts(context[1])["legacy_import_rollbacks"] == 1
    assert rollback(context).status == "rolled_back"


def test_failure_after_intent_rolls_back_sql_and_forbids_forward_replay(
    context, monkeypatch
):
    original = module._intent
    before = counts(context[1])

    def interrupted(*args, **kwargs):
        original(*args, **kwargs)
        raise RuntimeError("interrupt before commit")

    with monkeypatch.context() as patch:
        patch.setattr(module, "_intent", interrupted)
        with pytest.raises(MigrationError, match="hook failed"):
            rollback(context)
    assert counts(context[1]) == before
    assert all(p.exists() for p in finals(context))
    with pytest.raises(MigrationError, match="rollback journal exists"):
        apply(context)
    assert rollback(context).status == "rolled_back"


@pytest.mark.parametrize(
    "change",
    [
        "intent",
        "publication",
        "database",
        "new_pk",
        "file",
        "missing_sql",
        "new_reference",
    ],
)
def test_pending_cleanup_revalidates_all_evidence(context, monkeypatch, change):
    with monkeypatch.context() as patch:
        patch.setattr(
            module,
            "_remove_publication",
            lambda *args: (_ for _ in ()).throw(OSError()),
        )
        with pytest.raises(MigrationError):
            rollback(context)
    if change == "intent":
        (journal(context) / "rollback-intent.enc").write_bytes(b"tampered")
    elif change == "publication":
        (journal(context) / "publish-file-00000000.enc").unlink()
    elif change == "database":
        mutate(context[1], "DELETE FROM legacy_import_targets")
    elif change == "missing_sql":
        mutate(context[1], "DELETE FROM legacy_import_rollbacks")
    elif change == "file":
        finals(context)[1].write_bytes(b"changed after SQL commit")
    elif change == "new_reference":
        add(
            context, "chat_commands", dict(id="new-command", chat_id="c1", kind="steer")
        )
    else:
        add(context, "sessions", dict(id="s1", title="new user work"))
    with pytest.raises(MigrationError):
        rollback(context)
    assert all(p.exists() for p in finals(context))


@pytest.mark.parametrize(
    "options", [{"target_stopped": False}, {"key": b"x" * 32}, {"max_rows": 0}]
)
def test_invalid_selection_cannot_delete(context, options):
    before = counts(context[1])
    with pytest.raises(MigrationError):
        rollback(context, **options)
    assert counts(context[1]) == before
    assert all(p.exists() for p in finals(context))


_WORKER = """
import os, sys, json
from pathlib import Path
from qwenpaw_data.host.core.migration import rollback as module
path, home, url, action = sys.argv[1:]
if action == 'intent':
    original = module._intent
    def crash(*a, **kw):
        original(*a, **kw)
        os._exit(91)
    module._intent = crash
elif action == 'sql':
    original = module.rollback_sql_rows
    def crash(*a, **kw):
        original(*a, **kw)
        os._exit(92)
    module.rollback_sql_rows = crash
elif action in ('unlink', 'last-unlink'):
    original = module.os.unlink
    calls = []
    def crash(*a, **kw):
        original(*a, **kw)
        calls.append(True)
        if action == 'unlink' or len(calls) == 2:
            os._exit(93 if action == 'unlink' else 95)
    module.os.unlink = crash
elif action == 'complete':
    original = module._remove_publication
    def crash(*a, **kw):
        original(*a, **kw)
        os._exit(94)
    module._remove_publication = crash
result = module.rollback_saved_plan(Path(path), database_url=url,
    target_home=Path(home), key=bytes(range(32)), target_stopped=True)
print(json.dumps(result.report()), flush=True)
"""


@pytest.mark.parametrize(
    "action,code",
    [
        ("intent", 91),
        ("sql", 92),
        ("unlink", 93),
        ("complete", 94),
        ("last-unlink", 95),
    ],
)
def test_fresh_process_crash_recovery(context, action, code):
    args = [
        sys.executable,
        "-c",
        _WORKER,
        str(context[3]),
        str(context[0].target_home),
        context[1],
    ]
    crashed = subprocess.run(
        [*args, action], capture_output=True, text=True, timeout=30
    )
    assert crashed.returncode == code, crashed.stderr
    remaining = sum(p.exists() for p in finals(context))
    assert (
        remaining
        == {"intent": 2, "sql": 2, "unlink": 1, "complete": 0, "last-unlink": 0}[action]
    )
    retried = subprocess.run(
        [*args, "retry"], capture_output=True, text=True, timeout=30
    )
    assert retried.returncode == 0, retried.stderr
    result = json.loads(retried.stdout)
    assert result["published_files_absent"] and result["imported_rows_absent"]
    assert result["status"] == (
        "already_rolled_back" if action == "complete" else "rolled_back"
    )
    assert rollback(context).status == "already_rolled_back"
