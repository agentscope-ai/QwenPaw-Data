# -*- coding: utf-8 -*-
"""Tests for the async DB-backed ChannelConfigStore."""

from __future__ import annotations

from pathlib import Path

import pytest

from qwenpaw_data.host.core.channels.channel_config_store import (
    ChannelConfigStore,
    ChannelIdConflictError,
    find_identity_by_channel_id,
)
from qwenpaw_data.host.core.db.engine import (
    TEST_TENANT_ID,
    TEST_USER_ID,
    TEST_WORKSPACE_ID,
    init_db,
    reset_engine,
)
from qwenpaw_data.host.core.domain.identity import Identity

_IDENTITY = Identity(
    user_id=TEST_USER_ID,
    tenant_id=TEST_TENANT_ID,
    workspace_id=TEST_WORKSPACE_ID,
)


async def _seed_feishu(app_secret: str) -> ChannelConfigStore:
    """Write a feishu channel with app_id + real app_secret, return the store."""
    store = ChannelConfigStore(_IDENTITY)
    await store.update_channel(
        "feishu",
        {
            "enabled": False,
            "app_id": "cli_xxx",
            "app_secret": app_secret,
            "encrypt_key": "",
            "verification_token": "",
            "streaming_enabled": True,
        },
    )
    return store


@pytest.mark.asyncio
async def test_get_masked_does_not_expose_real_secret(tmp_path: Path) -> None:
    await reset_engine()
    await init_db()
    try:
        store = await _seed_feishu("FakeAppSecretForTests0000000Demo")

        masked = (await store.get_masked())["feishu"]

        assert masked["app_secret"] == "Fake****Demo"
        # real value unchanged
        assert (await store.load())["feishu"][
            "app_secret"
        ] == "FakeAppSecretForTests0000000Demo"
    finally:
        await reset_engine()


@pytest.mark.asyncio
async def test_masked_secret_echoed_back_does_not_overwrite_real(
    tmp_path: Path,
) -> None:
    await reset_engine()
    await init_db()
    try:
        store = await _seed_feishu("FakeAppSecretForTests0000000Demo")
        masked_echo = (await store.get_masked())["feishu"][
            "app_secret"
        ]  # "Fake****Demo"

        # frontend only toggles enabled, echoes the masked secret back unchanged
        await store.update_channel(
            "feishu",
            {"enabled": True, "app_secret": masked_echo},
        )

        persisted = (await store.load())["feishu"]
        assert persisted["app_secret"] == "FakeAppSecretForTests0000000Demo"
        assert persisted["enabled"] is True
    finally:
        await reset_engine()


@pytest.mark.asyncio
async def test_empty_secret_keeps_existing(tmp_path: Path) -> None:
    await reset_engine()
    await init_db()
    try:
        store = await _seed_feishu("real-secret-value")

        await store.update_channel("feishu", {"app_secret": "", "enabled": True})

        assert (await store.load())["feishu"]["app_secret"] == "real-secret-value"
    finally:
        await reset_engine()


@pytest.mark.asyncio
async def test_new_real_secret_overwrites(tmp_path: Path) -> None:
    await reset_engine()
    await init_db()
    try:
        store = await _seed_feishu("old-secret-value")

        await store.update_channel("feishu", {"app_secret": "brand-new-secret"})

        assert (await store.load())["feishu"]["app_secret"] == "brand-new-secret"
    finally:
        await reset_engine()


@pytest.mark.asyncio
async def test_first_time_set_secret_when_current_empty(tmp_path: Path) -> None:
    # current app_secret is empty; first-time set should write it
    await reset_engine()
    await init_db()
    try:
        store = ChannelConfigStore(_IDENTITY)
        await store.update_channel(
            "feishu",
            {
                "enabled": False,
                "app_id": "cli_xxx",
                "app_secret": "",
                "encrypt_key": "",
                "verification_token": "",
                "streaming_enabled": True,
            },
        )

        await store.update_channel("feishu", {"app_secret": "first-real-secret"})

        assert (await store.load())["feishu"]["app_secret"] == "first-real-secret"
    finally:
        await reset_engine()


@pytest.mark.asyncio
async def test_none_payload_value_is_skipped(tmp_path: Path) -> None:
    await reset_engine()
    await init_db()
    try:
        store = await _seed_feishu("real-secret-value")

        await store.update_channel("feishu", {"app_secret": None, "enabled": True})

        cfg = (await store.load())["feishu"]
        assert cfg["app_secret"] == "real-secret-value"
        assert cfg["enabled"] is True
    finally:
        await reset_engine()


@pytest.mark.asyncio
async def test_non_secret_fields_always_overwrite(tmp_path: Path) -> None:
    await reset_engine()
    await init_db()
    try:
        store = await _seed_feishu("real-secret-value")

        await store.update_channel(
            "feishu",
            {"app_id": "cli_new", "enabled": True, "streaming_enabled": False},
        )

        cfg = (await store.load())["feishu"]
        assert cfg["app_id"] == "cli_new"
        assert cfg["enabled"] is True
        assert cfg["streaming_enabled"] is False
        # secret not in payload, unchanged
        assert cfg["app_secret"] == "real-secret-value"
    finally:
        await reset_engine()


@pytest.mark.asyncio
async def test_test_channel_reports_missing_required_fields(tmp_path: Path) -> None:
    await reset_engine()
    await init_db()
    try:
        store = await _seed_feishu("")

        result = await store.test_channel("feishu")

        assert result["success"] is False
        assert "app_secret" in result["message"]
    finally:
        await reset_engine()


@pytest.mark.asyncio
async def test_test_channel_passes_when_required_present(tmp_path: Path) -> None:
    await reset_engine()
    await init_db()
    try:
        store = await _seed_feishu("real-secret-value")

        result = await store.test_channel("feishu")

        assert result["success"] is True
    finally:
        await reset_engine()


@pytest.mark.asyncio
async def test_load_returns_defaults_when_no_row(tmp_path: Path) -> None:
    await reset_engine()
    await init_db()
    try:
        store = ChannelConfigStore(_IDENTITY)
        cfg = await store.load()
        # all channels present, disabled
        assert cfg["feishu"]["enabled"] is False
        assert cfg["dingtalk"]["enabled"] is False
        assert cfg["wecom"]["enabled"] is False
    finally:
        await reset_engine()


@pytest.mark.asyncio
async def test_owner_identity_is_the_row_pk(tmp_path: Path) -> None:
    await reset_engine()
    await init_db()
    try:
        store = ChannelConfigStore(_IDENTITY)
        assert store.owner_identity() == _IDENTITY
    finally:
        await reset_engine()


_OTHER_IDENTITY = Identity(
    user_id="bob-user-id",
    tenant_id=TEST_TENANT_ID,
    workspace_id=TEST_WORKSPACE_ID,
)


async def _seed_feishu_for(identity: Identity, app_id: str, app_secret: str) -> None:
    """Write a feishu channel for an arbitrary identity."""
    store = ChannelConfigStore(identity)
    await store.update_channel(
        "feishu",
        {
            "enabled": True,
            "app_id": app_id,
            "app_secret": app_secret,
            "encrypt_key": "",
            "verification_token": "",
            "streaming_enabled": True,
        },
    )


@pytest.mark.asyncio
async def test_cross_identity_duplicate_app_id_rejected(tmp_path: Path) -> None:
    # Alice binds feishu app_id=cli_dup; Bob must not be able to bind the same app_id.
    await reset_engine()
    await init_db()
    try:
        await _seed_feishu_for(_IDENTITY, "cli_dup", "alice-secret")

        with pytest.raises(ChannelIdConflictError) as exc_info:
            await _seed_feishu_for(_OTHER_IDENTITY, "cli_dup", "bob-secret")

        assert exc_info.value.channel == "feishu"
        assert exc_info.value.field == "app_id"
        assert exc_info.value.value == "cli_dup"
        # Full message (for backend logs) names the owning user; the user-facing
        # message must not leak it.
        assert "503f" not in str(exc_info.value)
        assert "already bound to another user" in str(exc_info.value)
        # Bob's write was rejected — lookup still resolves to Alice.
        owner = await find_identity_by_channel_id("feishu", "cli_dup")
        assert owner == _IDENTITY
    finally:
        await reset_engine()


@pytest.mark.asyncio
async def test_same_identity_rebind_same_app_id_allowed(tmp_path: Path) -> None:
    # Re-saving one's own config with the same app_id is not a conflict.
    await reset_engine()
    await init_db()
    try:
        store = await _seed_feishu("alice-secret")  # app_id=cli_xxx via _seed_feishu

        # Toggling enabled while keeping app_id=cli_xxx must not raise.
        await store.update_channel("feishu", {"enabled": True})
        cfg = (await store.load())["feishu"]
        assert cfg["enabled"] is True
        assert cfg["app_id"] == "cli_xxx"
    finally:
        await reset_engine()


@pytest.mark.asyncio
async def test_clearing_app_id_does_not_conflict(tmp_path: Path) -> None:
    # Emptying the identity field is allowed (unbind) — no conflict check on empty values.
    await reset_engine()
    await init_db()
    try:
        store = await _seed_feishu("alice-secret")

        await store.update_channel("feishu", {"app_id": ""})

        assert (await store.load())["feishu"]["app_id"] == ""
    finally:
        await reset_engine()


@pytest.mark.asyncio
async def test_distinct_app_ids_both_allowed(tmp_path: Path) -> None:
    # Two identities binding different app_ids is fine.
    await reset_engine()
    await init_db()
    try:
        await _seed_feishu_for(_IDENTITY, "cli_alice", "alice-secret")
        await _seed_feishu_for(_OTHER_IDENTITY, "cli_bob", "bob-secret")

        assert await find_identity_by_channel_id("feishu", "cli_alice") == _IDENTITY
        assert await find_identity_by_channel_id("feishu", "cli_bob") == _OTHER_IDENTITY
    finally:
        await reset_engine()
