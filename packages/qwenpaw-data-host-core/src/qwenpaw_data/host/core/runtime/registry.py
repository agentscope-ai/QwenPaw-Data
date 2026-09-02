# -*- coding: utf-8 -*-
from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from qwenpaw_data.host.core.runtime.chat_runtime import ChatRuntime


class RuntimeRegistry:
    """In-process chat_id → in-flight ChatRuntime."""

    def __init__(self) -> None:
        self._by_chat: dict[str, ChatRuntime] = {}

    def register(self, chat_id: str, runtime: ChatRuntime) -> None:
        if chat_id in self._by_chat:
            raise RuntimeError(f"chat already running: {chat_id}")
        self._by_chat[chat_id] = runtime

    def unregister(self, chat_id: str) -> None:
        self._by_chat.pop(chat_id, None)

    def get(self, chat_id: str) -> ChatRuntime | None:
        return self._by_chat.get(chat_id)


_REGISTRY: RuntimeRegistry | None = None


def get_runtime_registry() -> RuntimeRegistry:
    global _REGISTRY
    if _REGISTRY is None:
        _REGISTRY = RuntimeRegistry()
    return _REGISTRY


def reset_runtime_registry() -> None:
    global _REGISTRY
    _REGISTRY = None
