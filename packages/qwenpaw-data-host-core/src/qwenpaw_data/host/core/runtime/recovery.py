# -*- coding: utf-8 -*-
from __future__ import annotations

import asyncio
import logging
from contextlib import suppress

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from qwenpaw_data.host.core.repositories.chat import ChatRepository
from qwenpaw_data.host.core.settings import get_settings

logger = logging.getLogger(__name__)


class ChatRecovery:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory
        self._task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        await self.run()
        self._task = asyncio.create_task(self._loop())

    async def shutdown(self) -> None:
        task = self._task
        self._task = None
        if task is None:
            return
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task

    async def run(self) -> None:
        async with self._session_factory() as db:
            await ChatRepository.recover_running(db)
            await db.commit()

    async def _loop(self) -> None:
        interval = get_settings().runtime.recover_interval_seconds
        while True:
            await asyncio.sleep(interval)
            try:
                await self.run()
            except Exception:
                logger.exception("recover running chats failed")
