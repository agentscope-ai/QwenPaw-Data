"""Publication is recoverable but never adopts unrelated target files."""

import os
import subprocess
import sys

import pytest

from qwenpaw_data.host.core.migration.legacy_json import MigrationError
from qwenpaw_data.host.core.migration.locking import migration_lock
from qwenpaw_data.host.core.migration.publication import (
    publish_files,
    verify_publication,
)
from qwenpaw_data.host.core.migration.sql_import import (
    commit_sql_rows,
    recover_sql_receipt,
)

from .test_legacy_recovery_bundle import KEY, context as bundle_context, save, load
from .test_legacy_file_staging import stage


pytestmark = pytest.mark.skipif(
    os.name != "posix", reason="POSIX workspace publication"
)


@pytest.fixture
def context(tmp_path):
    result = bundle_context.__wrapped__(tmp_path)
    save(result)
    stage(result)
    return result


def publish(context, *, verify=False):
    recovery = load(context)
    with migration_lock(recovery.plan.target_home, target_stopped=True) as lock:
        return (verify_publication if verify else publish_files)(
            recovery, lock=lock, database_url=context[1], key=KEY
        )


def commit(context, *, after_publish=lambda: None):
    recovery = load(context)
    with migration_lock(recovery.plan.target_home, target_stopped=True) as lock:

        def hook():
            publish_files(recovery, lock=lock, database_url=context[1], key=KEY)
            after_publish()

        result = commit_sql_rows(
            recovery.plan,
            database_url=context[1],
            run_id=recovery.run_id,
            target_key=KEY,
            target_anchor=recovery.target_anchor,
            target_stopped=True,
            recover_sqlite=True,
            before_commit=hook,
        )
        verify_publication(recovery, lock=lock, database_url=context[1], key=KEY)
        return result


def test_exact_publish_keeps_private_owned_stage_links_empty_directories_and_replays(
    context,
):
    plan = context[0]
    staged = stage(context)
    receipt = publish(context)
    assert receipt.files == 2 and not receipt.report()["sql_commit_verified"]
    for item in staged.files:
        final = plan.target_home / plan.files[item.index].destination
        assert final.samefile(item.path)
        assert final.stat().st_nlink == 2 and final.stat().st_mode & 0o7777 == 0o600
        assert (
            final.read_bytes()
            == (
                plan.source.home.parent / "backup" / plan.files[item.index].source
            ).read_bytes()
        )
    for relative in plan.directories:
        assert (plan.target_home / relative).is_dir()
    assert publish(context) == receipt == publish(context, verify=True)
    assert (
        recover_sql_receipt(
            plan,
            database_url=context[1],
            run_id=context[4],
            target_anchor=context[2],
            target_stopped=True,
        )
        is None
    )
    first = commit(context)
    assert first.status == "committed"
    assert commit(context).data_digest == first.data_digest


@pytest.mark.parametrize(
    "kind", ["same_bytes", "different_bytes", "symlink", "directory"]
)
def test_existing_target_is_never_adopted_or_overwritten_even_if_bytes_match(
    context, kind
):
    plan = context[0]
    target = plan.target_home / plan.files[-1].destination
    target.parent.mkdir(parents=True)
    if kind == "directory":
        target.mkdir()
    elif kind == "symlink":
        target.symlink_to(plan.source.home.parent / "backup" / plan.files[-1].source)
    else:
        target.write_bytes(
            (plan.source.home.parent / "backup" / plan.files[-1].source).read_bytes()
            if kind == "same_bytes"
            else b"preserve user bytes"
        )
    before = target.lstat()
    with pytest.raises(MigrationError):
        publish(context)
    assert target.lstat().st_ino == before.st_ino
    assert not (plan.target_home / plan.files[0].destination).exists()


def test_mid_publication_failure_preserves_first_file_and_resumes_with_same_inode(
    context, monkeypatch
):
    import qwenpaw_data.host.core.migration.publication as publication

    original = publication.os.link
    count = 0

    def fail_second(*args, **kwargs):
        nonlocal count
        count += 1
        if count == 2:
            raise OSError("synthetic link interruption")
        return original(*args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(publication.os, "link", fail_second)
        with pytest.raises(MigrationError, match="retain staging"):
            publish(context)
    first = context[0].target_home / context[0].files[0].destination
    inode = first.stat().st_ino
    assert publish(context).files == 2
    assert first.stat().st_ino == inode


def test_sql_hook_failure_leaves_owned_publication_for_exact_retry_not_cleanup(context):
    def fail():
        raise RuntimeError("after files before SQL commit")

    with pytest.raises(MigrationError, match="not committed"):
        commit(context, after_publish=fail)
    assert publish(context, verify=True).files == 2
    assert (
        recover_sql_receipt(
            context[0],
            database_url=context[1],
            run_id=context[4],
            target_anchor=context[2],
            target_stopped=True,
        )
        is None
    )
    assert commit(context).status == "committed"


@pytest.mark.parametrize(
    "change", ["content", "replacement", "extra_link", "missing_intent"]
)
def test_published_mutations_refuse_repair_or_overwrite(context, change):
    staged = stage(context)
    publish(context)
    item = staged.files[0]
    final = context[0].target_home / context[0].files[0].destination
    if change == "content":
        final.write_bytes(b"new user work")
    elif change == "replacement":
        other = final.with_name("replacement")
        other.write_bytes(final.read_bytes())
        other.chmod(0o600)
        other.replace(final)
    elif change == "extra_link":
        os.link(final, final.with_name("extra-link"))
    else:
        intent = item.path.parent.parent / "publish-file-00000000.enc"
        intent.rename(intent.with_name("retained-intent"))
    before = final.read_bytes()
    for verify in (False, True):
        with pytest.raises(MigrationError):
            publish(context, verify=verify)
    assert final.read_bytes() == before and item.path.exists()


def test_directory_replacement_and_symlink_ancestors_are_refused(context):
    plan = context[0]
    external = plan.target_home.parent / "external"
    external.mkdir()
    first_component = plan.directories[0].split("/")[0]
    link = plan.target_home / first_component
    link.symlink_to(external, target_is_directory=True)
    with pytest.raises(MigrationError):
        publish(context)
    assert not list(external.iterdir())
    link.unlink()
    publish(context)
    empty = next(plan.target_home / p for p in plan.directories if p.endswith("/empty"))
    empty.rename(empty.with_name("preserved-empty"))
    empty.mkdir()
    with pytest.raises(MigrationError, match="replaced"):
        publish(context)


def test_interrupted_journal_write_retains_temp_and_never_links_without_intent(
    context, monkeypatch
):
    import qwenpaw_data.host.core.migration.publication as publication

    original = publication.staging._write_record

    def fail(fd, name, document, cipher):
        if "destination" in document:
            out = os.open(name, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600, dir_fd=fd)
            os.write(out, b"partial encrypted journal")
            os.close(out)
            raise OSError("journal interrupted")
        return original(fd, name, document, cipher)

    with monkeypatch.context() as patch:
        patch.setattr(publication.staging, "_write_record", fail)
        with pytest.raises(MigrationError):
            publish(context)
    assert all(
        not (context[0].target_home / f.destination).exists() for f in context[0].files
    )
    root = context[0].target_home / ".legacy-migrations" / context[4]
    partial = list(root.glob(".publish-*.tmp"))
    assert len(partial) == 1
    assert publish(context).files == 2
    assert partial[0].read_bytes() == b"partial encrypted journal"


_WORKER = """
import os, sys
from pathlib import Path
from qwenpaw_data.host.core.migration.bundle import load_recovery_plan
from qwenpaw_data.host.core.migration.locking import migration_lock
from qwenpaw_data.host.core.migration.publication import publish_files, verify_publication
from qwenpaw_data.host.core.migration.sql_import import commit_sql_rows, recover_sql_receipt
import qwenpaw_data.host.core.migration.publication as publication
path, home, url, action = sys.argv[1:]
key = bytes(range(32))
recovery = load_recovery_plan(Path(path), database_url=url, target_home=Path(home), key=key)
with migration_lock(Path(home), target_stopped=True) as lock:
    old = recover_sql_receipt(recovery.plan, database_url=url, run_id=recovery.run_id,
                              target_anchor=recovery.target_anchor, target_stopped=True)
    if action == 'receipt':
        print('absent' if old is None else old.status, flush=True)
    else:
        if action == 'crash-link':
            original = publication.os.link
            def crash(*args, **kwargs):
                original(*args, **kwargs)
                os._exit(73)
            publication.os.link = crash
        def hook():
            publish_files(recovery, lock=lock, database_url=url, key=key)
            if action == 'crash-after-files':
                os._exit(74)
        result = commit_sql_rows(recovery.plan, database_url=url, run_id=recovery.run_id,
                                 target_anchor=recovery.target_anchor, target_key=key,
                                 target_stopped=True, recover_sqlite=True, before_commit=hook)
        if action == 'lost-response':
            os._exit(75)
        assert verify_publication(recovery, lock=lock, database_url=url, key=key).files == 2
        print(result.status, flush=True)
"""


@pytest.mark.parametrize(
    "action,code",
    [("crash-link", 73), ("crash-after-files", 74), ("lost-response", 75)],
)
def test_new_process_sql_file_crash_recovery_and_exact_retry(context, action, code):
    args = [
        sys.executable,
        "-c",
        _WORKER,
        str(context[3]),
        str(context[0].target_home),
        context[1],
    ]

    def child(mode, expected=0):
        result = subprocess.run(
            [*args, mode], capture_output=True, text=True, timeout=30
        )
        assert result.returncode == expected, result.stderr
        return result.stdout.strip()

    child(action, code)
    committed = action == "lost-response"
    assert child("receipt") == ("already_committed" if committed else "absent")
    assert child("retry") == ("already_committed" if committed else "committed")
    assert child("retry") == "already_committed"
    assert publish(context, verify=True).files == 2
