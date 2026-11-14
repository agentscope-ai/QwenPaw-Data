"""One saved-plan entry point must distinguish SQL outcome before file actions."""

import os
import subprocess
import sys

import pytest
from sqlalchemy import create_engine, text

from qwenpaw_data.host.core.migration.coordinator import apply_saved_plan
from qwenpaw_data.host.core.migration.legacy_json import MigrationError

from .test_legacy_recovery_bundle import KEY, context as bundle_context, save
from .test_legacy_target_validation import mutate


pytestmark = pytest.mark.skipif(
    os.name != "posix", reason="POSIX migration coordinator"
)


@pytest.fixture
def context(tmp_path):
    result = bundle_context.__wrapped__(tmp_path)
    save(result)
    return result


def apply(context, **overrides):
    kwargs = dict(
        database_url=context[1],
        target_home=context[0].target_home,
        key=KEY,
        target_stopped=True,
    )
    kwargs.update(overrides)
    return apply_saved_plan(context[3], **kwargs)


def test_saved_plan_drives_staging_sql_files_and_exact_repeat(context):
    plan = context[0]
    plan.source.home.rename(plan.source.home.with_name("source-offline"))
    first = apply(context)
    assert first.status == "committed" and first.files == 2
    assert first.rows == len(plan.rows)
    assert not first.report()["target_runtime_configuration_verified"]
    second = apply(context)
    assert (
        second.status == "already_committed" and second.data_digest == first.data_digest
    )
    assert second.plan_digest == first.plan_digest
    # Verifying an already committed import does not need backup access.
    backup = plan.source.home.parent / "backup"
    backup.rename(backup.with_name("backup-offline"))
    assert apply(context) == second


@pytest.mark.parametrize("wrong", ["key", "stop", "limits", "directory"])
def test_invalid_selection_does_not_create_lock_or_stage(context, tmp_path, wrong):
    kwargs = (
        {"key": b"x" * 32}
        if wrong == "key"
        else {"target_stopped": False}
        if wrong == "stop"
        else {"max_incomplete_attempts": True}
        if wrong == "limits"
        else {"target_home": tmp_path}
    )
    with pytest.raises(MigrationError):
        apply(context, **kwargs)
    assert not (context[0].target_home / ".legacy-migration.lock").exists()
    assert not (context[0].target_home / ".legacy-migrations").exists()


@pytest.mark.parametrize(
    "statement",
    [
        "UPDATE users SET status='inactive'",
        "DELETE FROM role_bindings",
        "DELETE FROM legacy_import_targets",
    ],
)
def test_inactive_identity_or_wrong_database_stops_before_staging(context, statement):
    mutate(context[1], statement)
    with pytest.raises(MigrationError):
        apply(context)
    assert not (context[0].target_home / ".legacy-migrations").exists()


def test_uncommitted_import_requires_verified_independent_backup(context):
    backup = context[0].source.home.parent / "backup"
    backup.rename(backup.with_name("backup-offline"))
    with pytest.raises(MigrationError):
        apply(context)
    assert not (context[0].target_home / ".legacy-migrations").exists()


def test_parent_alias_resolves_to_recorded_home_but_final_symlink_is_refused(
    context, tmp_path
):
    alias = tmp_path / "parent-alias"
    alias.symlink_to(context[0].target_home.parent, target_is_directory=True)
    assert (
        apply(context, target_home=alias / context[0].target_home.name).status
        == "committed"
    )
    final = tmp_path / "home-alias"
    final.symlink_to(context[0].target_home, target_is_directory=True)
    with pytest.raises(MigrationError, match="final symbolic link"):
        apply(context, target_home=final)


@pytest.mark.parametrize("change", ["file_changed", "file_missing", "row_changed"])
def test_committed_import_is_verified_not_repaired(context, change):
    apply(context)
    final = context[0].target_home / context[0].files[0].destination
    if change == "file_changed":
        final.write_bytes(b"new user work")
    elif change == "file_missing":
        final.rename(final.with_name("user-moved-file"))
    else:
        mutate(context[1], "UPDATE sessions SET title='new user title'")
    with pytest.raises(MigrationError):
        apply(context)
    if change == "file_changed":
        assert final.read_bytes() == b"new user work"
    elif change == "file_missing":
        assert not final.exists()
    else:
        engine = create_engine(context[1])
        with engine.connect() as conn:
            assert conn.scalar(text("SELECT title FROM sessions")) == "new user title"
        engine.dispose()


def test_ambiguous_commit_error_retains_files_and_next_invocation_verifies_receipt(
    context, monkeypatch
):
    import qwenpaw_data.host.core.migration.coordinator as coordinator

    original = coordinator.commit_sql_rows

    def lose_response(*args, **kwargs):
        original(*args, **kwargs)
        raise OSError("synthetic lost response")

    with monkeypatch.context() as patch:
        patch.setattr(coordinator, "commit_sql_rows", lose_response)
        with pytest.raises(MigrationError, match="preserve"):
            apply(context)
    assert all(
        (context[0].target_home / f.destination).exists() for f in context[0].files
    )
    assert apply(context).status == "already_committed"


def test_unavailable_sql_does_not_enter_staging_or_cleanup(context, monkeypatch):
    import qwenpaw_data.host.core.migration.coordinator as coordinator

    def unavailable(*args, **kwargs):
        raise MigrationError("synthetic unavailable database")

    monkeypatch.setattr(coordinator, "recover_sql_receipt", unavailable)
    with pytest.raises(MigrationError, match="unavailable"):
        apply(context)
    assert not (context[0].target_home / ".legacy-migrations").exists()


_WORKER = """
import os, sys, json
from pathlib import Path
from qwenpaw_data.host.core.migration.coordinator import apply_saved_plan
import qwenpaw_data.host.core.migration.coordinator as coordinator
import qwenpaw_data.host.core.migration.staging as staging
import qwenpaw_data.host.core.migration.publication as publication
path, home, url, action = sys.argv[1:]
if action == 'crash-copy':
    def crash(fd, backup, placement):
        out = os.open('data', os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600, dir_fd=fd)
        os.write(out, b'interrupted copy')
        os.fsync(out)
        os._exit(71)
    staging._copy_data = crash
elif action == 'crash-link':
    original = publication.os.link
    def crash(*args, **kwargs):
        original(*args, **kwargs)
        os._exit(72)
    publication.os.link = crash
elif action == 'crash-files':
    original = coordinator.publish_files
    def crash(*args, **kwargs):
        original(*args, **kwargs)
        os._exit(73)
    coordinator.publish_files = crash
elif action == 'lost-response':
    original = coordinator.commit_sql_rows
    def crash(*args, **kwargs):
        original(*args, **kwargs)
        os._exit(74)
    coordinator.commit_sql_rows = crash
result = apply_saved_plan(Path(path), database_url=url, target_home=Path(home),
                          key=bytes(range(32)), target_stopped=True)
print(json.dumps(result.report()), flush=True)
"""


@pytest.mark.parametrize(
    "action,code",
    [
        ("crash-copy", 71),
        ("crash-link", 72),
        ("crash-files", 73),
        ("lost-response", 74),
    ],
)
def test_saved_plan_coordinator_recovers_fresh_process_crashes(context, action, code):
    import json

    args = [
        sys.executable,
        "-c",
        _WORKER,
        str(context[3]),
        str(context[0].target_home),
        context[1],
    ]
    crash = subprocess.run([*args, action], capture_output=True, text=True, timeout=30)
    assert crash.returncode == code, crash.stderr
    result = subprocess.run(
        [*args, "retry"], capture_output=True, text=True, timeout=30
    )
    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout)
    assert report["verified_files"] == 2 and report["verified_rows"] == len(
        context[0].rows
    )
    assert report["status"] == (
        "already_committed" if action == "lost-response" else "committed"
    )
    assert apply(context).data_digest == report["data_digest"]
