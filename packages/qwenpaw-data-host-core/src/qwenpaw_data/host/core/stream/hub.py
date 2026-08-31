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

    async def subscribe_live(
        self,
        chat_id: str,
        *,
        heartbeat_interval: float | None = None,
    ) -> AsyncIterator[StreamObject | None]:
        """Yield live events; ``None`` is an optional transport heartbeat."""
        q: asyncio.Queue[StreamObject | object] = asyncio.Queue()
        self._subs[chat_id].add(q)
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
            self._subs[chat_id].discard(q)
            if not self._subs[chat_id]:
                del self._subs[chat_id]


_HUB: EventHub | None = None


def get_hub() -> EventHub:
    global _HUB
    if _HUB is None:
        _HUB = EventHub()
    return _HUB


def reset_hub() -> None:
    global _HUB
    _HUB = None
