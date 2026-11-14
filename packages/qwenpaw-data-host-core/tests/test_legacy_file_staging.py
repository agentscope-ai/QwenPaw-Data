"""Private file preparation must preserve originals and survive process exits."""

from dataclasses import replace
import os
import subprocess
import sys

import pytest
from sqlalchemy import create_engine, text

from qwenpaw_data.host.core.migration.legacy_json import MigrationError, scan_source
from qwenpaw_data.host.core.migration.locking import migration_lock
from qwenpaw_data.host.core.migration.staging import stage_files
from qwenpaw_data.host.core.migration.sql_import import commit_sql_rows

from .test_legacy_recovery_bundle import KEY, context as bundle_context, load, save


pytestmark = pytest.mark.skipif(os.name != "posix", reason="POSIX migration staging")


@pytest.fixture
def context(tmp_path):
    return bundle_context.__wrapped__(tmp_path)


def stage(context, **overrides):
    recovery = load(context)
    kwargs = dict(database_url=context[1], key=KEY)
    kwargs.update(overrides)
    with migration_lock(recovery.plan.target_home, target_stopped=True) as lock:
        return stage_files(recovery, lock=lock, **kwargs)


def test_exact_private_copies_repeat_without_source_or_native_workspace_writes(context):
    save(context)
    plan = context[0]
    before = scan_source(plan.source.home).digest
    source_offline = plan.source.home.with_name("offline-source")
    plan.source.home.rename(source_offline)
    first = stage(context)
    assert first.report()["verified_files"] == 2
    assert not first.report()["workspace_files_published"]
    assert not first.report()["sql_written"]
    assert first.incomplete_attempts == 0
    assert stage(context) == first
    backup = source_offline.parent / "backup"
    for file in first.files:
        placement = plan.files[file.index]
        assert file.path.read_bytes() == (backup / placement.source).read_bytes()
        assert file.path.stat().st_mode & 0o7777 == 0o600
        assert file.path.stat().st_nlink == 1
        assert not (plan.target_home / placement.destination).exists()
    assert scan_source(source_offline).digest == before
    engine = create_engine(context[1])
    with engine.connect() as conn:
        assert conn.scalar(text("SELECT count(*) FROM sessions")) == 0
        assert conn.scalar(text("SELECT count(*) FROM legacy_import_runs")) == 0
    engine.dispose()


def test_lock_reentrance_and_closed_handle_are_refused_without_unlinking(context):
    home = context[0].target_home
    with migration_lock(home, target_stopped=True) as held:
        held.check()
        inode = (home / ".legacy-migration.lock").stat().st_ino
        with pytest.raises(MigrationError, match="Another migration"):
            with migration_lock(home, target_stopped=True):
                pass
    with pytest.raises(MigrationError, match="no longer held"):
        held.check()
    with migration_lock(home, target_stopped=True):
        assert (home / ".legacy-migration.lock").stat().st_ino == inode
    with pytest.raises(MigrationError, match="stopped"):
        with migration_lock(home):
            pass


@pytest.mark.parametrize("kind", ["symlink", "hardlink", "fifo", "public", "nonempty"])
def test_unsafe_lock_files_are_never_repaired_or_removed(context, kind):
    home = context[0].target_home
    path = home / ".legacy-migration.lock"
    other = home / "preserve"
    other.write_bytes(b"user bytes")
    if kind == "symlink":
        path.symlink_to(other)
    elif kind == "hardlink":
        os.link(other, path)
    elif kind == "fifo":
        os.mkfifo(path, 0o600)
    else:
        path.write_bytes(b"" if kind == "public" else b"preserve")
        path.chmod(0o644 if kind == "public" else 0o600)
    before = path.lstat()
    with pytest.raises(MigrationError):
        with migration_lock(home, target_stopped=True):
            pass
    assert path.lstat().st_ino == before.st_ino
    assert other.read_bytes() == b"user bytes"


@pytest.mark.parametrize("change", ["key", "anchor", "digest"])
def test_wrong_recovery_binding_does_not_create_staging(context, change):
    save(context)
    recovery = load(context)
    if change != "key":
        recovery = replace(
            recovery,
            **{
                "target_anchor" if change == "anchor" else "digest": "f"
                * (32 if change == "anchor" else 64)
            },
        )
    with migration_lock(recovery.plan.target_home, target_stopped=True) as lock:
        with pytest.raises(MigrationError):
            stage_files(
                recovery,
                lock=lock,
                database_url=context[1],
                key=b"x" * 32 if change == "key" else KEY,
            )
    assert not (recovery.plan.target_home / ".legacy-migrations").exists()


def test_interrupted_copy_is_preserved_and_retry_uses_a_new_attempt(
    context, monkeypatch
):
    import qwenpaw_data.host.core.migration.staging as staging

    save(context)

    def fail(fd, backup, placement):
        out = os.open("data", os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600, dir_fd=fd)
        os.write(out, b"partial")
        os.close(out)
        raise OSError("synthetic copy interruption")

    with monkeypatch.context() as patch:
        patch.setattr(staging, "_copy_data", fail)
        with pytest.raises(MigrationError, match="retain all copies"):
            stage(context)
    root = context[0].target_home / ".legacy-migrations" / context[4]
    pending = list(root.glob("pending-*"))
    assert len(pending) == 1
    inode = (pending[0] / "data").stat().st_ino
    receipt = stage(context)
    assert receipt.incomplete_attempts == 1 and len(receipt.files) == 2
    assert (pending[0] / "data").read_bytes() == b"partial"
    assert (pending[0] / "data").stat().st_ino == inode


def test_incomplete_attempt_limit_stops_new_copies(context, monkeypatch):
    import qwenpaw_data.host.core.migration.staging as staging

    save(context)
    with monkeypatch.context() as patch:
        patch.setattr(
            staging, "_copy_data", lambda *args: (_ for _ in ()).throw(OSError())
        )
        with pytest.raises(MigrationError):
            stage(context, max_incomplete_attempts=1)
    with pytest.raises(MigrationError, match="attempt limit"):
        stage(context, max_incomplete_attempts=1)
    root = context[0].target_home / ".legacy-migrations" / context[4]
    assert len(list(root.glob("pending-*"))) == 1
    assert not list(root.glob("ready-*"))


@pytest.mark.parametrize(
    "change", ["data", "receipt", "same_bytes_new_inode", "hardlink", "unknown"]
)
def test_completed_staging_tampering_refuses_reuse_and_preserves_files(context, change):
    save(context)
    receipt = stage(context)
    data = receipt.files[0].path
    if change == "data":
        data.write_bytes(b"changed")
    elif change == "receipt":
        (data.parent / "receipt.enc").write_bytes(b"invalid")
    elif change == "same_bytes_new_inode":
        replacement = data.with_name("replacement")
        replacement.write_bytes(data.read_bytes())
        replacement.chmod(0o600)
        replacement.replace(data)
    elif change == "hardlink":
        os.link(data, context[0].target_home / "outside-alias")
    else:
        (data.parent / "preserve").write_bytes(b"user work")
    before = data.read_bytes()
    with pytest.raises(MigrationError):
        stage(context)
    assert data.read_bytes() == before


def test_changed_backup_and_committed_sql_are_refused_before_staging(context):
    save(context)
    plan = context[0]
    file = plan.source.home.parent / "backup" / plan.files[0].source
    before = file.read_bytes()
    file.write_bytes(b"changed")
    with pytest.raises(MigrationError, match="Backup content"):
        stage(context)
    assert not (plan.target_home / ".legacy-migrations").exists()
    file.write_bytes(before)
    commit_sql_rows(
        plan,
        database_url=context[1],
        run_id=context[4],
        target_key=KEY,
        target_anchor=context[2],
        target_stopped=True,
        before_commit=lambda: None,
    )
    with pytest.raises(MigrationError, match="SQL is already committed"):
        stage(context)
    assert not (plan.target_home / ".legacy-migrations").exists()


@pytest.mark.parametrize("name", [".Legacy-Migration.Lock", ".Legacy-Migrations"])
def test_portable_aliases_are_preserved_and_block_staging(context, name):
    save(context)
    alias = context[0].target_home / name
    alias.write_bytes(b"preserve user data")
    with pytest.raises(MigrationError, match="alias"):
        stage(context)
    assert alias.read_bytes() == b"preserve user data"
    assert not (context[0].target_home / ".legacy-migrations").is_dir()


def test_reserved_migration_namespace_cannot_be_used_as_tenant_scope(context):
    from .test_legacy_migration_plan import prepare
    from .test_legacy_record_migration import USERS

    users = {
        key: {**value, "tenant_id": ".legacy-migrations"}
        for key, value in USERS.items()
    }
    plan = prepare(context[0].source.home, users=users)
    changed = (plan, *context[1:])
    save(changed)
    with pytest.raises(MigrationError, match="reserved migration"):
        stage(changed)
    assert not (plan.target_home / ".legacy-migrations").exists()


def test_incomplete_root_initialization_is_not_repaired_or_adopted(
    context, monkeypatch
):
    import qwenpaw_data.host.core.migration.staging as staging

    save(context)
    original = staging._write_record

    def interrupt(fd, name, document, cipher):
        if name == "binding.enc":
            raise OSError("initialization interrupted")
        return original(fd, name, document, cipher)

    with monkeypatch.context() as patch:
        patch.setattr(staging, "_write_record", interrupt)
        with pytest.raises(MigrationError):
            stage(context)
    root = context[0].target_home / ".legacy-migrations" / context[4]
    before = root.stat().st_ino
    with pytest.raises(MigrationError):
        stage(context)
    assert root.stat().st_ino == before and not list(root.iterdir())


def test_relocated_staging_directory_is_not_trusted_even_with_original_receipts(
    context,
):
    import shutil

    save(context)
    receipt = stage(context)
    root = receipt.files[0].path.parent.parent
    original = root.with_name("retained-original")
    root.rename(original)
    shutil.copytree(original, root)
    with pytest.raises(MigrationError, match="different plan or directory"):
        stage(context)
    assert root.exists() and original.exists()


def test_lock_replacement_is_detected_without_removing_either_file(context):
    home = context[0].target_home
    with pytest.raises(MigrationError, match="changed or is unsafe"):
        with migration_lock(home, target_stopped=True):
            path = home / ".legacy-migration.lock"
            path.rename(home / "retained-lock")
            path.touch(mode=0o600)
    assert path.exists() and (home / "retained-lock").exists()


_WORKER = """
import os, sys
from pathlib import Path
from qwenpaw_data.host.core.migration.bundle import load_recovery_plan
from qwenpaw_data.host.core.migration.locking import migration_lock
from qwenpaw_data.host.core.migration.staging import stage_files
import qwenpaw_data.host.core.migration.staging as staging
path, home, url, action = sys.argv[1:]
key = bytes(range(32))
plan = load_recovery_plan(Path(path), database_url=url, target_home=Path(home), key=key)
with migration_lock(Path(home), target_stopped=True) as lock:
    if action == "hold":
        print("locked", flush=True)
        sys.stdin.readline()
    elif action == "crash":
        def crash(fd, backup, placement):
            out = os.open("data", os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600, dir_fd=fd)
            os.write(out, b"partial before process exit")
            os.fsync(out)
            os._exit(73)
        staging._copy_data = crash
        stage_files(plan, lock=lock, database_url=url, key=key)
    else:
        receipt = stage_files(plan, lock=lock, database_url=url, key=key)
        print(len(receipt.files), receipt.incomplete_attempts, flush=True)
"""


def test_fresh_process_lock_exclusion_exit_release_and_staging_recovery(context):
    save(context)
    args = [
        sys.executable,
        "-c",
        _WORKER,
        str(context[3]),
        str(context[0].target_home),
        context[1],
    ]
    child = subprocess.Popen(
        [*args, "hold"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        assert child.stdout.readline().strip() == "locked"
        with pytest.raises(MigrationError, match="Another migration"):
            with migration_lock(context[0].target_home, target_stopped=True):
                pass
        child.kill()
        child.communicate(timeout=20)
    finally:
        if child.poll() is None:
            child.kill()
            child.communicate(timeout=20)
    crash = subprocess.run([*args, "crash"], capture_output=True, text=True, timeout=30)
    assert crash.returncode == 73, crash.stderr
    completed = subprocess.run(
        [*args, "stage"], capture_output=True, text=True, timeout=30
    )
    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip() == "2 1"
    assert stage(context).incomplete_attempts == 1
