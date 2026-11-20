"""Synthetic keys only; credential conversion must not consult ambient settings."""

from __future__ import annotations

import base64
from copy import deepcopy

from cryptography.fernet import Fernet, InvalidToken
import pytest

from qwenpaw_data.host.core.migration.credentials import CredentialRewriter
from qwenpaw_data.host.core.migration.legacy_json import MigrationError


OLD = bytes(range(32))
NEW = bytes(range(32, 64))
SECRET = " synthetic-凭据-with-exact-whitespace "


def cipher(key):
    return Fernet(base64.urlsafe_b64encode(key[:32]))


def old_token(value=SECRET, key=OLD):
    return "ENC:" + cipher(key).encrypt(value.encode()).decode()


@pytest.fixture
async def db_session_factory(tmp_path):
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
    from qwenpaw_data.host.core.db.tables import Base

    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'credentials.db'}")
    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        yield async_sessionmaker(engine, expire_on_commit=False)
    finally:
        await engine.dispose()


@pytest.mark.parametrize("source_key", [OLD, OLD + b"legacy-ignored-suffix"])
@pytest.mark.parametrize("encrypted", [True, False])
def test_exact_secret_survives_rotation(source_key, encrypted):
    value = old_token() if encrypted else SECRET
    converted = CredentialRewriter(source_key=source_key, target_key=NEW).rewrite(value)
    assert converted.startswith("ENC:") and converted != value
    assert cipher(NEW).decrypt(converted[4:].encode()).decode() == SECRET
    with pytest.raises(InvalidToken):
        cipher(OLD).decrypt(converted[4:].encode())


def test_plaintext_requires_no_source_key_and_repeated_conversion_is_authenticated():
    converted = CredentialRewriter(source_key=None, target_key=NEW).rewrite(SECRET)
    # Even when source and destination match, never pass an ENC token through.
    rotated = CredentialRewriter(source_key=NEW, target_key=NEW).rewrite(converted)
    assert converted != rotated
    assert cipher(NEW).decrypt(rotated[4:].encode()).decode() == SECRET
    with pytest.raises(MigrationError, match="explicit legacy"):
        CredentialRewriter(source_key=None, target_key=NEW).rewrite(converted)


@pytest.mark.parametrize(
    "value",
    [
        None,
        3,
        b"secret",
        "",
        " \t",
        "\ud800",
        "ENC:bad-token",
        "ENC:凭据",
        old_token(""),
        old_token(" \n"),
    ],
)
def test_malformed_credentials_never_become_empty_or_pass_through(value):
    with pytest.raises(MigrationError) as exc:
        CredentialRewriter(source_key=OLD, target_key=NEW).rewrite(value)
    assert SECRET not in str(exc.value)
    assert "bad-token" not in str(exc.value)
    assert exc.value.__suppress_context__ or exc.value.__context__ is None


def test_wrong_key_and_non_utf8_plaintext_fail_closed():
    converter = CredentialRewriter(source_key=OLD, target_key=NEW)
    for value in [old_token(key=NEW), "ENC:" + cipher(OLD).encrypt(b"\xff").decode()]:
        with pytest.raises(MigrationError, match="authenticate or decode") as exc:
            converter.rewrite(value)
        assert value not in str(exc.value)


@pytest.mark.parametrize(
    "source,target",
    [
        (b"short", NEW),
        ("a" * 32, NEW),
        (OLD, b"short"),
        (OLD, NEW + b"extra"),
        (OLD, NEW.hex()),
        (OLD, None),
    ],
)
def test_invalid_master_keys_are_rejected_without_disclosing_them(source, target):
    with pytest.raises(MigrationError, match="master key"):
        CredentialRewriter(source_key=source, target_key=target)


@pytest.mark.parametrize("limit", [True, 0, -1, 1.5])
def test_invalid_limits(limit):
    with pytest.raises(MigrationError, match="limit"):
        CredentialRewriter(source_key=OLD, target_key=NEW, max_bytes=limit)


@pytest.mark.parametrize("value", ["x" * 9, "凭据凭据", old_token()])
def test_credentials_are_bounded_in_bytes(value):
    with pytest.raises(MigrationError, match="size limit"):
        CredentialRewriter(source_key=OLD, target_key=NEW, max_bytes=8).rewrite(value)


def test_provider_conversion_is_all_or_nothing_and_does_not_mutate_source():
    providers = {
        "openai": {
            "api_key_enc": SECRET,
            "base_url": "https://fixture.invalid/?key=private",
        },
        "dashscope": {"api_key_enc": old_token(), "base_url": None},
    }
    original = deepcopy(providers)
    converter = CredentialRewriter(source_key=OLD, target_key=NEW)
    result = converter.rewrite_providers(providers)
    assert result.plaintext_count == result.encrypted_count == 1
    assert providers == original
    for record in result.providers.values():
        assert (
            cipher(NEW).decrypt(record["api_key_enc"][4:].encode()).decode() == SECRET
        )
    assert result.providers["openai"]["base_url"] == original["openai"]["base_url"]
    assert "private" not in repr(result) and SECRET not in repr(result)
    assert NEW.hex() not in repr(converter)
    providers["bad"] = {"api_key_enc": "ENC:broken-secret"}
    with pytest.raises(MigrationError):
        converter.rewrite_providers(providers)
    assert {k: v for k, v in providers.items() if k != "bad"} == original


@pytest.mark.parametrize(
    "providers",
    [
        [],
        {"../escape": {"api_key_enc": SECRET}},
        {"openai": None},
        {"openai": {}},
        {"openai": {"api_key_enc": SECRET, "future_secret": "do-not-drop"}},
        {"openai": {"api_key_enc": SECRET, "base_url": 3}},
    ],
)
def test_unknown_or_invalid_provider_records_are_not_silently_lost(providers):
    with pytest.raises(MigrationError):
        CredentialRewriter(source_key=OLD, target_key=NEW).rewrite_providers(providers)


def test_conversion_never_reads_runtime_settings_or_environment(monkeypatch):
    from qwenpaw_data.host.core import settings

    def forbidden():
        pytest.fail("offline conversion consulted runtime settings")

    monkeypatch.setattr(settings, "get_settings", forbidden)
    monkeypatch.setenv("QWENPAW_DATA_PREFERENCES_MASTER_SECRET", OLD.hex())
    monkeypatch.setenv("QWENPAW_DATA_PREFS_MASTER_SECRET", NEW.hex())
    converted = CredentialRewriter(source_key=OLD, target_key=NEW).rewrite(old_token())
    assert cipher(NEW).decrypt(converted[4:].encode()).decode() == SECRET


async def test_converted_rows_reload_through_current_sql_repository(
    db_session_factory, monkeypatch
):
    from qwenpaw_data.host.core.db.tables import UserProviderRow
    from qwenpaw_data.host.core.domain.identity import Identity
    from qwenpaw_data.host.core.repositories.preferences import PreferencesRepository
    from qwenpaw_data.host.core.settings import clear_settings

    monkeypatch.setenv("QWENPAW_DATA_PREFERENCES_MASTER_SECRET", NEW.hex())
    clear_settings()
    identity = Identity(user_id="migrated-user", tenant_id="t", workspace_id="w")
    result = CredentialRewriter(source_key=OLD, target_key=NEW).rewrite_providers(
        {
            "openai": {
                "api_key_enc": old_token(),
                "base_url": "https://fixture.invalid/v1",
            },
        }
    )
    async with db_session_factory() as db:
        async with db.begin():
            for pid, values in result.providers.items():
                db.add(
                    UserProviderRow(user_id=identity.user_id, provider_id=pid, **values)
                )
    async with db_session_factory() as db:
        loaded = await PreferencesRepository(db, identity).load()
        assert loaded.providers["openai"].api_key == SECRET
        assert loaded.providers["openai"].base_url == "https://fixture.invalid/v1"
        stored = await db.get(UserProviderRow, (identity.user_id, "openai"))
        assert (
            stored.api_key_enc.startswith("ENC:") and SECRET not in stored.api_key_enc
        )
