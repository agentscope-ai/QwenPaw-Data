# -*- coding: utf-8 -*-
"""Per-agent Steer queue: FIFO, ack, and terminal cancellation."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Literal

from qwenpaw_data.host.core.utils.ids import create_id

SteerStatus = Literal["waiting", "injecting", "injected", "cancelled"]


class SteerError(Exception):
    """Base error for Steer queue operations."""


class SteerChatEndedError(SteerError):
    """Chat ended (or queue closed) before the steer could be injected."""

    def __init__(self, message: str = "active chat has ended") -> None:
        super().__init__(message)


class SteerStateError(SteerError):
    """Illegal or duplicate Steer state transition."""


@dataclass
class PendingSteer:
    text: str
    id: str = field(default_factory=lambda: create_id("steer"))
    status: SteerStatus = "waiting"
    done: asyncio.Event = field(default_factory=asyncio.Event)


class SteerQueue:
    """Single-runtime FIFO of pending steers.

    Bound to one Agent / Chat lifetime. Does not store session_id or
    chat_id and is not a process-wide singleton.

    Mutations below the first ``await`` are synchronous, so concurrent
    coroutines on the same event loop cannot interleave mid-update.
    """

    def __init__(self) -> None:
        self._items: list[PendingSteer] = []
        self._closed = False

    @property
    def closed(self) -> bool:
        return self._closed

    async def wait_until_injected(self, text: str) -> None:
        """Enqueue ``text`` and wait until it is injected (or cancelled)."""
        if not text.strip():
            raise ValueError("text is required")
        item = PendingSteer(text=text)
        if self._closed:
            raise SteerChatEndedError()
        self._items.append(item)
        await item.done.wait()
        if item.status == "cancelled":
            raise SteerChatEndedError()
        if item.status != "injected":
            raise SteerStateError(
                f"steer finished in unexpected status: {item.status}",
            )

    async def take_pending(self) -> list[PendingSteer]:
        """Take all ``waiting`` items FIFO and mark them ``injecting``."""
        if self._closed:
            return []
        taken = [item for item in self._items if item.status == "waiting"]
        for item in taken:
            item.status = "injecting"
        return list(taken)

    async def mark_injected(self, item: PendingSteer) -> None:
        """Complete an ``injecting`` item; illegal completions fail fast."""
        if item not in self._items:
            raise SteerStateError("unknown steer item")
        if item.status != "injecting":
            raise SteerStateError(
                f"cannot mark injected from status={item.status}",
            )
        item.status = "injected"
        self._items = [x for x in self._items if x is not item]
        item.done.set()

    async def cancel_all(self) -> None:
        """Cancel every ``waiting``/``injecting`` item and close the queue."""
        self._closed = True
        pending = [
            item
            for item in self._items
            if item.status in ("waiting", "injecting")
        ]
        self._items.clear()
        for item in pending:
            item.status = "cancelled"
            item.done.set()
