# -*- coding: utf-8 -*-
"""Built-in channel registry (lazy import so a missing SDK doesn't break import)."""

from __future__ import annotations

import logging
from typing import Callable

from qwenpaw_data.host.core.channels.base import BaseChannel
from qwenpaw_data.host.core.channels.schema import ChannelType
from qwenpaw_data.host.core.domain.identity import Identity

logger = logging.getLogger("qwenpaw_data.channels.registry")

ChannelFactory = Callable[[], BaseChannel]

_BUILTIN: dict[str, ChannelFactory] = {}


def build_channel(identity: Identity, channel_type: str) -> BaseChannel | None:
    """Build a channel instance for ``(identity, channel_type)`` from the registry.

    Returns ``None`` (and logs) when no factory exists or the channel SDK is not
    installed. Wires ``owner_identity`` onto the instance.
    """
    factory = _BUILTIN.get(channel_type)
    if not factory:
        logger.error(f"build_channel: invalid type {channel_type}")
        return None
    try:
        ch = factory()
    except ImportError:
        logger.exception(f"build_channel: SDK not installed {channel_type}")
        return None
    ch.set_owner_identity(identity)
    logger.info(f"build_channel: built {channel_type} for identity {identity}")
    return ch


def _register_builtins() -> None:
    """Lazily register built-in channels."""

    def _register(key: str, factory: ChannelFactory) -> None:
        _BUILTIN[key] = factory

    def _feishu() -> BaseChannel:
        from qwenpaw_data.host.core.channels.feishu.channel import FeishuChannel

        return FeishuChannel()

    def _dingtalk() -> BaseChannel:
        from qwenpaw_data.host.core.channels.dingtalk.channel import DingTalkChannel

        return DingTalkChannel()

    def _wecom() -> BaseChannel:
        from qwenpaw_data.host.core.channels.wecom.channel import WecomChannel

        return WecomChannel()

    def _wechat() -> BaseChannel:
        from qwenpaw_data.host.core.channels.wechat.channel import WeChatChannel

        return WeChatChannel()

    _register(ChannelType.FEISHU.value, _feishu)
    _register(ChannelType.DINGTALK.value, _dingtalk)
    _register(ChannelType.WECOM.value, _wecom)
    _register(ChannelType.WECHAT.value, _wechat)


_register_builtins()
