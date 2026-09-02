# -*- coding: utf-8 -*-
from __future__ import annotations

import asyncio
from collections import defaultdict
from collections.abc import AsyncIterator
from typing import cast

from qwenpaw_data.host.core.api.models.stream_objects import StreamObject


class EventHub:
    """In-process async fan-out. Replay is done by the ChatEventStore before live."""

    def __init__(self) -> None:
        self._close_signal = object()
        self._subs: dict[
            str,
            set[asyncio.Queue[StreamObject | object]],
        ] = defaultdict(set)

    async def publish(self, chat_id: str, obj: StreamObject) -> None:
        for q in list(self._subs.get(chat_id, ())):
            await q.put(obj)

    async def close(self, chat_id: str) -> None:
        for q in list(self._subs.get(chat_id, ())):
            await q.put(self._close_signal)

    def attach(self, chat_id: str) -> asyncio.Queue[StreamObject | object]:
        """Register a subscriber queue synchronously.

        Use before replaying history so events published meanwhile are
        buffered instead of lost; consume with :meth:`iterate`.
        """
        q: asyncio.Queue[StreamObject | object] = asyncio.Queue()
        self._subs[chat_id].add(q)
        return q

    def detach(
        self,
        chat_id: str,
        q: asyncio.Queue[StreamObject | object],
    ) -> None:
        self._subs[chat_id].discard(q)
        if not self._subs[chat_id]:
            del self._subs[chat_id]

    async def iterate(
        self,
        chat_id: str,
        q: asyncio.Queue[StreamObject | object],
        *,
        heartbeat_interval: float | None = None,
    ) -> AsyncIterator[StreamObject | None]:
        """Drain an attached queue; ``None`` is an optional transport heartbeat."""
        try:
            while True:
                try:
                    item = await (
                        q.get()
                        if heartbeat_interval is None
                        else asyncio.wait_for(q.get(), timeout=heartbeat_interval)
                    )
                except TimeoutError:
                    yield None
                    continue
                if item is self._close_signal:
                    return
                yield cast(StreamObject, item)
        finally:
            self.detach(chat_id, q)

    async def subscribe_live(
        self,
        chat_id: str,
        *,
        heartbeat_interval: float | None = None,
    ) -> AsyncIterator[StreamObject | None]:
        """Yield live events; ``None`` is an optional transport heartbeat."""
        q = self.attach(chat_id)
        async for item in self.iterate(
            chat_id,
            q,
            heartbeat_interval=heartbeat_interval,
        ):
            yield item


_HUB: EventHub | None = None


def get_hub() -> EventHub:
    global _HUB
    if _HUB is None:
        _HUB = EventHub()
    return _HUB


def reset_hub() -> None:
    global _HUB
    _HUB = None
