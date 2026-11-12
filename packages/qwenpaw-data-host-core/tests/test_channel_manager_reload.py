# -*- coding: utf-8 -*-
"""Tests for ChannelManager.reload() — the core of reconnect-after-save."""

from __future__ import annotations

import asyncio
import contextlib
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from qwenpaw_data.host.core.channels.manager import ChannelManager
from qwenpaw_data.host.core.domain.identity import Identity
from qwenpaw_data.host.core.settings import clear_settings

_IDENTITY = Identity(user_id="u1", tenant_id="t1", workspace_id="w1")


class _FakeChannel:
    """Minimal channel stub: records start/stop calls."""

    def __init__(self, key: str) -> None:
        self.channel = key
        self.started = False
        self.stopped = False
        self._loop: Any = None
        self.owner_identity: Any = None

    def set_loop(self, loop: Any) -> None:
        self._loop = loop

    def set_owner_identity(self, identity: Any) -> None:
        self.owner_identity = identity

    async def start(self) -> None:
        self.started = True

    async def stop(self) -> None:
        self.stopped = True

    async def _stop_session_queues(self) -> int:
        return 0


@contextlib.contextmanager
def _config_ctx(factories: dict[str, Any], config: dict[str, Any]):
    """Patch the manager's build_channel + config-store helpers to return controlled config."""
    store = MagicMock()
    store.load = AsyncMock(return_value=config)

    def _build(identity: Any, key: str) -> Any:
        factory = factories.get(key)
        if factory is None:
            return None
        ch = factory()
        ch.set_owner_identity(identity)
        return ch

    with (
        patch(
            "qwenpaw_data.host.core.channels.manager.build_channel",
            side_effect=_build,
        ),
        patch(
            "qwenpaw_data.host.core.channels.manager.get_channel_config_store",
            return_value=store,
        ),
        patch(
            "qwenpaw_data.host.core.channels.manager.iter_identities",
            new=AsyncMock(return_value=[_IDENTITY]),
        ),
    ):
        yield


@pytest.mark.asyncio
async def test_reload_stops_old_starts_new_with_changed_config() -> None:
    feishu_v1 = _FakeChannel("feishu")
    with _config_ctx(
        {"feishu": lambda: feishu_v1},
        {"feishu": {"enabled": True}, "dingtalk": {"enabled": False}},
    ):
        mgr = await ChannelManager.from_config()
        await mgr.start_all()

    assert feishu_v1.started
    assert feishu_v1.owner_identity is _IDENTITY

    dingtalk = _FakeChannel("dingtalk")
    with _config_ctx(
        {"dingtalk": lambda: dingtalk},
        {"feishu": {"enabled": False}, "dingtalk": {"enabled": True}},
    ):
        result = await mgr.reload()

    assert feishu_v1.stopped
    assert (_IDENTITY, "feishu") not in mgr._channels
    assert mgr._channels[(_IDENTITY, "dingtalk")] is dingtalk
    assert dingtalk.started
    assert result == {
        "stopped": [f"{_IDENTITY.user_id}/feishu"],
        "started": [f"{_IDENTITY.user_id}/dingtalk"],
    }


@pytest.mark.asyncio
async def test_reload_recreates_channel_instance() -> None:
    """reload recreates the instance: even for the same channel key, a new instance is built (new secret takes effect)."""
    cfg = {"feishu": {"enabled": True}}
    ch_v1 = _FakeChannel("feishu")
    with _config_ctx({"feishu": lambda: ch_v1}, cfg):
        mgr = await ChannelManager.from_config()
        await mgr.start_all()

    ch_v2 = _FakeChannel("feishu")
    with _config_ctx({"feishu": lambda: ch_v2}, cfg):
        await mgr.reload()

    assert ch_v1.stopped
    assert mgr._channels[(_IDENTITY, "feishu")] is ch_v2
    assert ch_v2.started


@pytest.mark.asyncio
async def test_reload_preserves_loop_and_enqueue() -> None:
    """After reload the new channel still has the host loop bound (the _loop is reused)."""
    cfg = {"feishu": {"enabled": True}}
    with _config_ctx({"feishu": lambda: _FakeChannel("feishu")}, cfg):
        mgr = await ChannelManager.from_config()
        await mgr.start_all()
        assert mgr._loop is asyncio.get_running_loop()

        ch_after = _FakeChannel("feishu")
        with _config_ctx({"feishu": lambda: ch_after}, cfg):
            await mgr.reload()

    assert ch_after._loop is not None


@pytest.mark.asyncio
async def test_targeted_reload_only_touches_the_named_channel() -> None:
    """reload(identity, channel) stops/rebuilds only that channel; others keep running."""
    feishu = _FakeChannel("feishu")
    wecom = _FakeChannel("wecom")
    cfg = {"feishu": {"enabled": True}, "wecom": {"enabled": True}}
    with _config_ctx({"feishu": lambda: feishu, "wecom": lambda: wecom}, cfg):
        mgr = await ChannelManager.from_config()
        await mgr.start_all()

    assert feishu.started and wecom.started

    # Reload only feishu: feishu is rebuilt, wecom is untouched (not stopped, still present).
    feishu_v2 = _FakeChannel("feishu")
    with _config_ctx(
        {"feishu": lambda: feishu_v2, "wecom": lambda: _FakeChannel("wecom")}, cfg
    ):
        result = await mgr.reload(_IDENTITY, "feishu")

    assert feishu.stopped
    assert not wecom.stopped  # wecom was not touched
    assert mgr._channels[(_IDENTITY, "feishu")] is feishu_v2
    assert mgr._channels[(_IDENTITY, "wecom")] is wecom  # same instance retained
    assert feishu_v2.started
    assert result == {
        "stopped": [f"{_IDENTITY.user_id}/feishu"],
        "started": [f"{_IDENTITY.user_id}/feishu"],
    }


@pytest.mark.asyncio
async def test_targeted_reload_disables_channel_when_no_longer_enabled() -> None:
    """reload(identity, channel) when the channel is now disabled: stop + remove, start nothing."""
    feishu = _FakeChannel("feishu")
    with _config_ctx({"feishu": lambda: feishu}, {"feishu": {"enabled": True}}):
        mgr = await ChannelManager.from_config()
        await mgr.start_all()

    with _config_ctx(
        {"feishu": lambda: _FakeChannel("feishu")}, {"feishu": {"enabled": False}}
    ):
        result = await mgr.reload(_IDENTITY, "feishu")

    assert feishu.stopped
    assert (_IDENTITY, "feishu") not in mgr._channels
    assert result == {"stopped": [f"{_IDENTITY.user_id}/feishu"], "started": []}


@pytest.mark.asyncio
async def test_skips_enabled_channel_outside_allowlist(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("QWENPAW_DATA_CHANNELS", "dingtalk")
    clear_settings()
    feishu = _FakeChannel("feishu")
    dingtalk = _FakeChannel("dingtalk")
    with _config_ctx(
        {"feishu": lambda: feishu, "dingtalk": lambda: dingtalk},
        {"feishu": {"enabled": True}, "dingtalk": {"enabled": True}},
    ):
        mgr = await ChannelManager.from_config()

    assert (_IDENTITY, "feishu") not in mgr._channels
    assert mgr._channels[(_IDENTITY, "dingtalk")] is dingtalk
