# -*- coding: utf-8 -*-
"""Send queued scores once to the configured backend."""

from __future__ import annotations

import asyncio
import logging
from contextlib import suppress

import httpx
from qwenpaw_data.host.core.db.tables import ScoreDeliveryRow
from qwenpaw_data.host.core.settings import get_settings
from qwenpaw_data.host.core.utils.time import utcnow
from qwenpaw_data.tracing import Settings
from qwenpaw_data.tracing.export.install import scores_endpoint
from qwenpaw_data.tracing.export.score import delete_score, post_score, score_body
from qwenpaw_data.tracing.runtime.host.chat_id import trace_id_from_chat_id
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

logger = logging.getLogger(__name__)

# 超过此次数仍发不出去的行标记 sent_at，不再占队头窗口。
MAX_DELIVERY_ATTEMPTS = 10


def _mark_dead(row: ScoreDeliveryRow, reason: str) -> None:
    row.attempts += 1
    if row.attempts >= MAX_DELIVERY_ATTEMPTS:
        row.sent_at = utcnow()
        logger.error(
            "score delivery abandoned id=%s name=%s after %s attempts: %s",
            row.id,
            row.name,
            row.attempts,
            reason,
        )


async def flush_scores(session_factory: async_sessionmaker[AsyncSession]) -> None:
    settings = Settings.from_env()
    if not settings.configured():
        return
    url = scores_endpoint(settings.host)
    auth = (settings.public_key, settings.secret_key)
    async with session_factory() as db:
        rows = list(
            (
                await db.scalars(
                    select(ScoreDeliveryRow)
                    .where(ScoreDeliveryRow.sent_at.is_(None))
                    .order_by(ScoreDeliveryRow.created_at)
                    .limit(50)
                )
            ).all()
        )
        for row in rows:
            db.expunge(row)
    if not rows:
        return
    async with httpx.AsyncClient(timeout=10.0) as client:
        for row in rows:
            try:
                trace_id = None
                # replace：同 id 不能二次 POST，先删再发新值
                if row.op in {"delete", "replace"}:
                    await delete_score(client, url, auth, row.id)
                if row.op != "delete":
                    session_id = row.session_id
                    if not session_id:
                        # trace id 从 chat id 同源推导；老 uuid chat 推导为 None，直接死信。
                        trace_id = trace_id_from_chat_id(row.chat_id)
                        if trace_id is None:
                            row.attempts = MAX_DELIVERY_ATTEMPTS
                            _mark_dead(row, "legacy chat id, no derivable trace id")
                            continue
                    await post_score(
                        client,
                        url,
                        auth,
                        score_body(row, trace_id),
                    )
                    if row.op == "replace":
                        row.op = "create"
            except Exception:
                logger.exception("score delivery failed id=%s", row.id)
                _mark_dead(row, "delivery error")
                continue
            row.sent_at = utcnow()
    async with session_factory() as db:
        for row in rows:
            persisted = await db.get(ScoreDeliveryRow, row.id)
            if persisted is None:
                continue
            persisted.attempts = row.attempts
            persisted.sent_at = row.sent_at
            persisted.op = row.op
        await db.commit()


async def score_flush_loop(
    session_factory: async_sessionmaker[AsyncSession],
    stop: asyncio.Event,
) -> None:
    interval = get_settings().tracing.flush_interval_seconds
    while not stop.is_set():
        try:
            await flush_scores(session_factory)
        except Exception:
            logger.exception("score flush failed")
        try:
            await asyncio.wait_for(stop.wait(), interval)
        except TimeoutError:
            continue


class TracerScoreFlusher:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory
        self._stop = asyncio.Event()
        self._task: asyncio.Task[None] | None = None

    def start(self) -> None:
        if not Settings.from_env().configured():
            return
        self._task = asyncio.create_task(
            score_flush_loop(self._session_factory, self._stop)
        )

    async def shutdown(self, timeout: float = 15.0) -> None:
        self._stop.set()
        task = self._task
        self._task = None
        if task is None:
            return
        try:
            await asyncio.wait_for(task, timeout=timeout)
        except TimeoutError:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
