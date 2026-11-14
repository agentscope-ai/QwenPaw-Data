# -*- coding: utf-8 -*-
from __future__ import annotations

import asyncio
from collections import defaultdict
from collections.abc import AsyncGenerator, AsyncIterator
from contextlib import aclosing
from typing import cast

from qwenpaw_data.host.core.api.models.stream_objects import StreamObject


class EventHub:
    """In-process async fan-out. Replay is done by ChatEventRepository before live.

    A consumer that replays persisted events and then follows live ones must
    :meth:`attach` *before* the replay read and drain with :meth:`iterate`
    afterwards: events published in between are buffered on the queue instead
    of lost, and the overlap is deduplicated by ``sequence_number``.
    """

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
        """Register a subscriber queue synchronously; consume it with :meth:`iterate`."""
        q: asyncio.Queue[StreamObject | object] = asyncio.Queue()
        self._subs[chat_id].add(q)
        return q

    def detach(
        self,
        chat_id: str,
        q: asyncio.Queue[StreamObject | object],
    ) -> None:
        subs = self._subs.get(chat_id)
        if subs is None:
            return
        subs.discard(q)
        if not subs:
            del self._subs[chat_id]

    async def iterate(
        self,
        chat_id: str,
        q: asyncio.Queue[StreamObject | object],
        *,
        heartbeat_interval: float | None = None,
    ) -> AsyncGenerator[StreamObject | None, None]:
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
        """Yield live events; ``None`` is an optional transport heartbeat.

        Subscribes on the first iteration. Replaying consumers use
        :meth:`attach` + :meth:`iterate` instead.
        """
        q = self.attach(chat_id)
        async with aclosing(
            self.iterate(chat_id, q, heartbeat_interval=heartbeat_interval)
        ) as items:
            async for item in items:
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
