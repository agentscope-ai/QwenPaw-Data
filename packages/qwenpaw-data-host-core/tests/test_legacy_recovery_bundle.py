"""Authenticated private plans preserve exact conversion and target ownership."""

import json
import os
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, text

from qwenpaw_data.host.core.migration.binding import (
    database_binding,
    ensure_target_anchor,
)
from qwenpaw_data.host.core.migration.bundle import (
    load_recovery_plan,
    save_recovery_plan,
)
from qwenpaw_data.host.core.migration.legacy_json import MigrationError, scan_source
from qwenpaw_data.host.core.migration.sql_import import (
    commit_sql_rows,
    plan_digest,
    read_sql_receipt,
    recover_sql_receipt,
)

from .test_legacy_migration_plan import prepare, source as make_source
from .test_legacy_target_validation import initialize_target, mutate


KEY = bytes(range(32))
pytestmark = pytest.mark.skipif(
    os.name != "posix", reason="POSIX private recovery plan publication"
)


@pytest.fixture
def context(tmp_path):
    source = make_source.__wrapped__(tmp_path)
    plan = prepare(source)
    plan.target_home.mkdir(mode=0o700)
    url = initialize_target(tmp_path / "target.sqlite")
    anchor = ensure_target_anchor(url, target_stopped=True)
    folder = tmp_path / "journal"
    folder.mkdir(mode=0o700)
    return plan, url, anchor, folder / "plan.enc", uuid4().hex


def save(context, **overrides):
    plan, url, anchor, path, run_id = context
    kwargs = dict(
        backup=plan.source.home.parent / "backup",
        database_url=url,
        run_id=run_id,
        target_anchor=anchor,
        key=KEY,
        source_stopped=True,
        target_stopped=True,
    )
    kwargs.update(overrides)
    return save_recovery_plan(path, plan, **kwargs)


def load(context, **overrides):
    plan, url, _, path, _ = context
    kwargs = dict(database_url=url, target_home=plan.target_home, key=KEY)
    kwargs.update(overrides)
    return load_recovery_plan(path, **kwargs)


def test_private_roundtrip_preserves_ciphertext_timestamps_paths_and_exact_replay(
    context,
):
    plan, url, anchor, path, run_id = context
    source_before = scan_source(plan.source.home).digest
    assert save(context) == "created_and_verified"
    assert path.stat().st_mode & 0o777 == 0o600
    assert path.stat().st_nlink == 1
    data = path.read_bytes()
    for private in (
        b"private question",
        b"synthetic-private-key",
        KEY.hex().encode(),
        url.encode(),
    ):
        assert private not in data
    assert save(context) == "already_verified" and path.read_bytes() == data
    restored = load(context)
    assert restored.plan == plan
    assert restored.digest == plan_digest(plan) and restored.run_id == run_id
    assert restored.target_anchor == anchor
    assert "synthetic-private-key" not in repr(restored)
    assert scan_source(plan.source.home).digest == source_before
    result = commit_sql_rows(
        restored.plan,
        database_url=url,
        run_id=restored.run_id,
        target_key=KEY,
        target_anchor=restored.target_anchor,
        target_stopped=True,
        before_commit=lambda: None,
    )
    assert result.status == "committed"
    restored = load(context)
    receipt = read_sql_receipt(
        restored.plan,
        database_url=url,
        run_id=run_id,
        target_anchor=restored.target_anchor,
        target_stopped=True,
    )
    assert receipt.data_digest == result.data_digest
    assert (
        commit_sql_rows(
            restored.plan,
            database_url=url,
            run_id=run_id,
            target_anchor=anchor,
            target_key=KEY,
            target_stopped=True,
            before_commit=lambda: None,
        ).status
        == "already_committed"
    )


def test_anchor_is_stable_but_missing_or_wrong_database_is_not_an_absent_receipt(
    context, tmp_path
):
    plan, url, anchor, _, run_id = context
    assert ensure_target_anchor(url, target_stopped=True) == anchor
    other = initialize_target(tmp_path / "other.sqlite")
    for selected in (url, other):
        with pytest.raises(MigrationError, match="token"):
            read_sql_receipt(
                plan,
                database_url=selected,
                run_id=run_id,
                target_anchor="f" * 32,
                target_stopped=True,
            )
    with pytest.raises(MigrationError, match="token"):
        recover_sql_receipt(
            plan,
            database_url=other,
            run_id=run_id,
            target_anchor=anchor,
            target_stopped=True,
        )
    assert ensure_target_anchor(other, target_stopped=True) != anchor


def test_missing_recorded_token_blocks_import_instead_of_creating_one(context):
    plan, url, anchor, _, run_id = context
    mutate(url, "DELETE FROM legacy_import_targets")
    with pytest.raises(MigrationError, match="token"):
        commit_sql_rows(
            plan,
            database_url=url,
            run_id=run_id,
            target_anchor=anchor,
            target_key=KEY,
            target_stopped=True,
            before_commit=lambda: None,
        )
    engine = create_engine(url)
    with engine.connect() as conn:
        assert conn.scalar(text("SELECT count(*) FROM legacy_import_targets")) == 0
        assert conn.scalar(text("SELECT count(*) FROM sessions")) == 0
    engine.dispose()


def test_different_existing_bundle_is_never_replaced(context):
    save(context)
    before = context[3].read_bytes()
    with pytest.raises(MigrationError, match="not be replaced"):
        save(context, run_id=uuid4().hex)
    assert context[3].read_bytes() == before


@pytest.mark.parametrize(
    "change", ["wrong_key", "ciphertext", "truncation", "public", "extra_link"]
)
def test_unsafe_or_unauthenticated_bundle_refused(context, change):
    save(context)
    path = context[3]
    if change == "wrong_key":
        with pytest.raises(MigrationError, match="authenticate"):
            load(context, key=bytes(range(32, 64)))
        return
    if change == "ciphertext":
        data = bytearray(path.read_bytes())
        data[-15] ^= 1
        path.write_bytes(data)
    elif change == "truncation":
        path.write_bytes(path.read_bytes()[:40])
    elif change == "public":
        path.chmod(0o644)
    else:
        os.link(path, path.with_name("other-link"))
    with pytest.raises(MigrationError):
        load(context)


def test_changed_sqlite_file_or_target_home_requires_review(context):
    save(context)
    plan, url, _, _, _ = context
    other_home = plan.target_home.with_name("other-home")
    other_home.mkdir(mode=0o700)
    with pytest.raises(MigrationError, match="home"):
        load(context, target_home=other_home)
    database = Path(url.removeprefix("sqlite:///"))
    database.rename(database.with_suffix(".saved"))
    initialize_target(database)
    with pytest.raises(MigrationError, match="database selection"):
        load(context)


def test_loading_does_not_connect_to_database_or_read_backup(context, monkeypatch):
    import qwenpaw_data.host.core.migration.target as target

    save(context)
    source = context[0].source.home
    source.rename(source.with_name("source-offline"))
    backup = source.parent / "backup"
    backup.rename(backup.with_name("backup-offline"))
    monkeypatch.setattr(
        target, "create_engine", lambda *a, **k: pytest.fail("unexpected connection")
    )
    assert load(context).digest == plan_digest(context[0])


def test_postgres_binding_excludes_url_password_but_includes_connection_identity():
    original = database_binding("postgresql://user:secret@db.example/app", KEY)
    assert (
        database_binding("postgresql+psycopg://user:rotated@DB.EXAMPLE:5432/app", KEY)
        == original
    )
    assert database_binding("postgresql://other:secret@db.example/app", KEY) != original
    assert (
        database_binding("postgresql://user:secret@db.example/other", KEY) != original
    )


def test_bounds_stop_ack_and_independent_backup_are_mandatory(context):
    with pytest.raises(MigrationError, match="stopped"):
        save(context, source_stopped=False)
    with pytest.raises(MigrationError, match="byte limit"):
        save(context, max_bytes=100)
    assert not context[3].exists()
    save(context)
    with pytest.raises(MigrationError, match="limit"):
        load(context, max_bytes=100)


@pytest.mark.parametrize("where", ["envelope", "snapshot", "row", "traversal"])
def test_even_authenticated_unsupported_shapes_or_paths_are_refused(context, where):
    import qwenpaw_data.host.core.migration.bundle as bundle

    save(context)
    path = context[3]
    raw = path.read_bytes()
    document = json.loads(bundle._cipher(KEY).decrypt(raw[len(bundle._MAGIC) :]))
    if where == "envelope":
        document["unknown"] = "private-value"
    elif where == "snapshot":
        document["plan"]["source"]["unknown"] = "private-value"
    elif where == "row":
        document["plan"]["rows"][0]["unknown"] = "private-value"
    else:
        document["plan"]["files"][0]["destination"] = "../private-value"
    path.write_bytes(
        bundle._MAGIC + bundle._cipher(KEY).encrypt(json.dumps(document).encode())
    )
    with pytest.raises(MigrationError) as raised:
        load(context)
    assert "private-value" not in str(raised.value)


def test_in_place_database_replacement_is_caught_by_persistent_token(context, tmp_path):
    save(context)
    plan, url, _, _, run_id = context
    other = tmp_path / "other.sqlite"
    other_url = initialize_target(other)
    ensure_target_anchor(other_url, target_stopped=True)
    original = Path(url.removeprefix("sqlite:///"))
    original.write_bytes(other.read_bytes())  # Same pathname/inode, different database.
    restored = load(context)
    with pytest.raises(MigrationError, match="token"):
        read_sql_receipt(
            plan,
            database_url=url,
            run_id=run_id,
            target_anchor=restored.target_anchor,
            target_stopped=True,
        )


def test_source_change_blocks_publication_without_touching_existing_bundle(context):
    save(context)
    before = context[3].read_bytes()
    (context[0].source.home / "host/chats/c1/events.jsonl").write_text("changed")
    with pytest.raises(MigrationError, match="Source changed"):
        save(context)
    assert context[3].read_bytes() == before


def test_temporary_filename_collision_never_deletes_an_unowned_file(
    context, monkeypatch
):
    import qwenpaw_data.host.core.migration.bundle as bundle
    from types import SimpleNamespace

    existing = context[3].parent / (".migration-plan-" + "0" * 32 + ".tmp")
    existing.write_bytes(b"preserve existing file")
    monkeypatch.setattr(bundle, "uuid4", lambda: SimpleNamespace(hex="0" * 32))
    with pytest.raises(MigrationError, match="publication failed"):
        save(context)
    assert existing.read_bytes() == b"preserve existing file"
    assert not context[3].exists()


@pytest.mark.parametrize("failure", [1, 2])
def test_interrupted_publication_never_replaces_or_publishes_partial_data(
    context, monkeypatch, failure
):
    import qwenpaw_data.host.core.migration.bundle as bundle

    original = bundle.os.fsync
    calls = 0

    def fail(fd):
        nonlocal calls
        calls += 1
        if calls == failure:
            raise OSError("fixture sync failure")
        original(fd)

    with monkeypatch.context() as patch:
        patch.setattr(bundle.os, "fsync", fail)
        with pytest.raises(MigrationError, match="publication failed"):
            save(context)
    path = context[3]
    if failure == 1:
        assert not path.exists() and not list(path.parent.iterdir())
    else:
        assert list(path.parent.iterdir()) == [path]
        assert load(context).digest == plan_digest(context[0])
        assert save(context) == "already_verified"


def test_recovery_plan_key_is_domain_separated_from_provider_encryption(context):
    from cryptography.fernet import Fernet, InvalidToken
    import base64
    import qwenpaw_data.host.core.migration.bundle as bundle

    save(context)
    ciphertext = context[3].read_bytes()[len(bundle._MAGIC) :]
    with pytest.raises(InvalidToken):
        Fernet(base64.urlsafe_b64encode(KEY)).decrypt(ciphertext)
