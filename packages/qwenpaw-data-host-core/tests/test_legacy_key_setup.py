"""Local migration key setup must preserve existing secrets and SQL state."""

import base64
import json
import os
import stat

from cryptography.fernet import Fernet
import pytest
from sqlalchemy import create_engine, insert, text

from qwenpaw_data.host.core.db.tables import Base
from qwenpaw_data.host.core.migration.key_setup import (
    provision_local_key,
    verify_selected_runtime_key,
)
from qwenpaw_data.host.core.migration.legacy_json import MigrationError


KEY = bytes(range(32))
OTHER = bytes(range(32, 64))

pytestmark = pytest.mark.skipif(
    os.name != "posix",
    reason="POSIX key publication; native Windows ACL acceptance remains pending",
)


def token(key=KEY, value=b"synthetic-secret"):
    return "ENC:" + Fernet(base64.urlsafe_b64encode(key)).encrypt(value).decode()


@pytest.fixture
def target(tmp_path):
    home = tmp_path / "target home"
    home.mkdir(mode=0o700)
    database = tmp_path / "target.sqlite"
    url = f"sqlite:///{database}"
    engine = create_engine(url)
    Base.metadata.create_all(engine)
    engine.dispose()
    return home, database, url


def store(url, value, user="existing-user"):
    engine = create_engine(url)
    with engine.begin() as conn:
        conn.execute(
            insert(Base.metadata.tables["user_providers"]),
            dict(
                user_id=user,
                provider_id="openai",
                api_key_enc=value,
                base_url="https://fixture.invalid",
            ),
        )
    engine.dispose()


def setup(target, **kwargs):
    return provision_local_key(
        target[0],
        key=kwargs.pop("key", KEY),
        database_url=target[2],
        target_stopped=kwargs.pop("target_stopped", True),
        **kwargs,
    )


@pytest.mark.parametrize(
    "mode",
    ["local", "primary", "alias", "mismatch", "invalid", "production", "missing"],
)
def test_read_only_runtime_key_selection_never_generates_keys(
    target, monkeypatch, mode
):
    for name in (
        "QWENPAW_DATA_PREFERENCES_MASTER_SECRET",
        "QWENPAW_DATA_PREFS_MASTER_SECRET",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("QWENPAW_DATA_ENV", "development")
    home, database, _ = target
    if mode == "local":
        setup(target)
    elif mode == "production":
        setup(target)
        monkeypatch.setenv("QWENPAW_DATA_ENV", "production")
    elif mode in {"primary", "alias", "mismatch", "invalid"}:
        name = (
            "QWENPAW_DATA_PREFS_MASTER_SECRET"
            if mode == "alias"
            else "QWENPAW_DATA_PREFERENCES_MASTER_SECRET"
        )
        monkeypatch.setenv(
            name,
            "private-invalid-secret"
            if mode == "invalid"
            else OTHER.hex()
            if mode == "mismatch"
            else KEY.hex(),
        )
    before = {p.name: p.read_bytes() for p in home.iterdir()}
    sql = database.read_bytes()
    if mode in {"local", "primary", "alias"}:
        assert verify_selected_runtime_key(home, key=KEY) == (
            "explicit_target_home_file"
            if mode == "local"
            else "current_process_environment"
        )
    else:
        with pytest.raises(MigrationError) as error:
            verify_selected_runtime_key(home, key=KEY)
        assert "private-invalid-secret" not in str(error.value)
        assert OTHER.hex() not in str(error.value)
    assert {p.name: p.read_bytes() for p in home.iterdir()} == before
    assert database.read_bytes() == sql


@pytest.mark.parametrize("selection", ["relative", "alias", "traversal"])
def test_runtime_environment_secret_does_not_bypass_explicit_target_selection(
    target, monkeypatch, selection
):
    from pathlib import Path

    home = target[0]
    alias = home.with_name("alias")
    alias.symlink_to(home, target_is_directory=True)
    selected = (
        Path("relative-home")
        if selection == "relative"
        else alias
        if selection == "alias"
        else home / ".." / home.name
    )
    monkeypatch.setenv("QWENPAW_DATA_PREFERENCES_MASTER_SECRET", KEY.hex())
    with pytest.raises(MigrationError, match="explicit non-root absolute home"):
        verify_selected_runtime_key(selected, key=KEY)
    assert not (home / "preferences.key").exists()


def test_publish_complete_private_key_is_idempotent_and_runtime_compatible(
    target, monkeypatch
):
    from qwenpaw_data.host.core.settings.security import SecuritySettings

    home, database, url = target
    store(url, token())
    before = database.read_bytes()
    result = setup(target)
    assert result.status == "created_and_verified"
    assert result.encrypted_credentials_checked == 1
    file = home / "preferences.key"
    assert file.read_text() == KEY.hex()
    assert stat.S_IMODE(file.stat().st_mode) == 0o600
    assert file.stat().st_nlink == 1
    assert list(home.iterdir()) == [file]
    assert database.read_bytes() == before
    stamp = file.stat().st_mtime_ns
    assert setup(target).status == "already_verified"
    assert file.stat().st_mtime_ns == stamp
    assert KEY.hex() not in json.dumps(result.report()) + repr(result)
    assert result.report()["target_runtime_configuration_verified"] is False
    assert result.report()["import_ready"] is False
    monkeypatch.setenv("QWENPAW_DATA_HOME", str(home))
    monkeypatch.delenv("QWENPAW_DATA_PREFERENCES_MASTER_SECRET", raising=False)
    monkeypatch.delenv("QWENPAW_DATA_PREFS_MASTER_SECRET", raising=False)
    assert SecuritySettings(env="development").master_key() == KEY
    assert file.stat().st_mtime_ns == stamp


@pytest.mark.parametrize(
    "value",
    [
        token(OTHER),
        "ENC:corrupt",
        token(value=b"\xff"),
        token(value=b" "),
        "",
        "x" * 65537,
    ],
    ids=["wrong-key", "corrupt", "non-utf8", "blank", "empty", "oversized"],
)
def test_wrong_key_or_invalid_credentials_prevent_any_key_creation(target, value):
    home, database, url = target
    store(url, value)
    before = database.read_bytes()
    with pytest.raises(MigrationError) as raised:
        setup(target)
    assert not list(home.iterdir())
    assert database.read_bytes() == before
    assert KEY.hex() not in str(raised.value)
    if value:
        assert value not in str(raised.value)


def test_all_existing_users_are_checked_not_only_migration_users(target):
    store(target[2], token(), "first-user")
    store(target[2], token(OTHER), "unrelated-user")
    with pytest.raises(MigrationError, match="authenticate"):
        setup(target)
    assert not list(target[0].iterdir())


def test_existing_plaintext_is_counted_but_never_rewritten(target):
    store(target[2], "synthetic-plaintext")
    before = target[1].read_bytes()
    result = setup(target)
    assert result.plaintext_credentials_found == 1
    assert result.encrypted_credentials_checked == 0
    assert target[1].read_bytes() == before
    assert "synthetic-plaintext" not in json.dumps(result.report())


@pytest.mark.parametrize("value", [OTHER.hex(), "malformed", "", KEY.hex() + "00"])
def test_existing_different_or_invalid_key_is_never_replaced(target, value):
    file = target[0] / "preferences.key"
    file.write_text(value)
    file.chmod(0o600)
    with pytest.raises(MigrationError, match="not be replaced"):
        setup(target)
    assert file.read_text() == value
    assert list(target[0].iterdir()) == [file]


@pytest.mark.parametrize(
    "unsafe", ["public", "symlink", "hardlink", "directory", "fifo"]
)
def test_unsafe_existing_key_refused_without_repair(target, tmp_path, unsafe):
    file = target[0] / "preferences.key"
    if unsafe in {"public", "hardlink"}:
        file.write_text(KEY.hex())
        file.chmod(0o644 if unsafe == "public" else 0o600)
        if unsafe == "hardlink":
            os.link(file, tmp_path / "other-link")
    elif unsafe == "symlink":
        external = tmp_path / "external.key"
        external.write_text(KEY.hex())
        external.chmod(0o600)
        file.symlink_to(external)
    elif unsafe == "directory":
        file.mkdir()
    else:
        os.mkfifo(file, 0o600)
    before = file.lstat()
    with pytest.raises(MigrationError, match="private"):
        setup(target)
    assert file.lstat() == before


def test_missing_database_does_not_create_key_or_database(target):
    home, _, _ = target
    missing = home / "absent.sqlite"
    with pytest.raises(MigrationError, match="existing"):
        provision_local_key(
            home, key=KEY, database_url=f"sqlite:///{missing}", target_stopped=True
        )
    assert not list(home.iterdir())


def test_missing_schema_is_not_created(target):
    engine = create_engine(target[2])
    with engine.begin() as conn:
        conn.execute(text("DROP TABLE user_providers"))
    engine.dispose()
    before = target[1].read_bytes()
    with pytest.raises(MigrationError, match="schema"):
        setup(target)
    assert target[1].read_bytes() == before
    assert not list(target[0].iterdir())


def test_explicit_stop_ack_and_existing_safe_home_are_mandatory(target):
    with pytest.raises(MigrationError, match="stopped"):
        setup(target, target_stopped=False)
    target[0].chmod(0o777)
    with pytest.raises(MigrationError, match="writable"):
        setup(target)
    assert not list(target[0].iterdir())


@pytest.mark.parametrize("same", [True, False])
def test_concurrent_publication_is_checked_not_replaced(target, monkeypatch, same):
    import qwenpaw_data.host.core.migration.key_setup as module

    original = module.os.link
    file = target[0] / "preferences.key"

    def racing_link(*args, **kwargs):
        file.write_text((KEY if same else OTHER).hex())
        file.chmod(0o600)
        return original(*args, **kwargs)

    monkeypatch.setattr(module.os, "link", racing_link)
    if same:
        assert setup(target).status == "already_verified"
    else:
        with pytest.raises(MigrationError, match="differs"):
            setup(target)
    assert file.read_text() == (KEY if same else OTHER).hex()
    assert list(target[0].iterdir()) == [file]


def test_interrupted_write_does_not_publish_partial_key(target, monkeypatch):
    import qwenpaw_data.host.core.migration.key_setup as module

    def fail_sync(fd):
        raise OSError("simulated disk failure")

    monkeypatch.setattr(module.os, "fsync", fail_sync)
    with pytest.raises(MigrationError, match="failed"):
        setup(target)
    assert not list(target[0].iterdir())


def test_failure_after_publication_keeps_complete_key_and_allows_verified_retry(
    target, monkeypatch
):
    import qwenpaw_data.host.core.migration.key_setup as module

    original = module.os.fsync
    calls = 0

    def fail_directory_sync(fd):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("simulated directory sync failure")
        original(fd)

    with monkeypatch.context() as patch:
        patch.setattr(module.os, "fsync", fail_directory_sync)
        with pytest.raises(MigrationError, match="failed"):
            setup(target)
    file = target[0] / "preferences.key"
    assert list(target[0].iterdir()) == [file]
    assert file.read_text() == KEY.hex()
    assert setup(target).status == "already_verified"


def test_credential_scan_bounds_record_count(target):
    from qwenpaw_data.host.core.migration.key_setup import check_database_credentials
    from qwenpaw_data.host.core.migration.target import _read_only_target

    store(target[2], token(), "one")
    store(target[2], token(), "two")
    before = target[1].read_bytes()
    with _read_only_target(target[2]) as conn:
        with pytest.raises(MigrationError, match="record limit"):
            check_database_credentials(conn, KEY, max_records=1)
        assert check_database_credentials(conn, KEY, max_records=2) == (2, 0)
    assert target[1].read_bytes() == before


def test_environment_override_and_production_requirement_are_not_changed(
    target, monkeypatch
):
    from qwenpaw_data.host.core.settings.security import SecuritySettings

    setup(target)
    monkeypatch.setenv("QWENPAW_DATA_HOME", str(target[0]))
    monkeypatch.setenv("QWENPAW_DATA_PREFERENCES_MASTER_SECRET", OTHER.hex())
    assert SecuritySettings(env="production").master_key() == OTHER
    with pytest.raises(RuntimeError, match="must be set in production"):
        SecuritySettings(env="production", preferences_master_secret="").master_key()
    assert (target[0] / "preferences.key").read_text() == KEY.hex()
