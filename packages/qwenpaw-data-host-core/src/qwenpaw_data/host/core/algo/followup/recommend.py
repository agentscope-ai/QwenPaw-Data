# -*- coding: utf-8 -*-
"""The algorithm side of the FollowUp contract: one recommender per Chat.

Everything the host sees is here: the constructor the host calls and the three
lifecycle calls. ``join`` hands back the questions rather than emitting them, so
the host keeps ownership of the stream and its ordering.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

from qwenpaw_data.host.core.algo.followup.collector import SignalCollector
from qwenpaw_data.host.core.algo.followup.llm import FollowUpLLM, for_structured_calls
from qwenpaw_data.host.core.algo.followup.models import Candidate, FollowUp, SignalSnapshot
from qwenpaw_data.host.core.algo.followup.service import FollowUpService
from qwenpaw_data.host.core.algo.followup.settings import (
    MAX_DIMENSIONS,
    MAX_METRICS,
    MAX_QUESTIONS,
    MIN_RELEVANCE,
    TIMEOUT_SEC,
)
from qwenpaw_data.host.core.model import build_model_from_env
from qwenpaw_data.host.core.providers.factory import build_model

if TYPE_CHECKING:
    # Typing only: the host's runtime package imports this module through its
    # middleware, so importing it back at import time would close a cycle.
    from qwenpaw_data.host.core.runtime.context import RunContext

logger = logging.getLogger(__name__)

FollowUpCallback = Callable[[list[str]], Awaitable[None]]


class FollowUpRecommend:
    """Recommend the next questions for one Chat, out of the reply's way.

    The host drives ``start`` once, ``append`` per event, and ``join`` on the
    completed path only. None of the three may block the reply: collection runs
    on its own task, and ``join`` gives up on its own budget rather than holding
    the turn open.

    Every knob is a plain argument with the validated default: the host owns the
    configuration (``settings.followup``) and passes it in, so nothing here
    reads the environment on its own. Whether recommendation runs at all is a
    host decision; this class is only constructed when the host wants it.

    Args:
        run_context: The Chat being answered, and the models it may run on.
        previous_followups: Questions recommended earlier in this Session, so
            the same one is not offered twice.
        deliver: Optional sink for a result that misses the budget. Without it a
            late result is dropped, since the turn has already closed.
        timeout_sec: What the closing moment of the Chat will wait for.
        max_questions: Upper bound on the delivered questions.
        max_metrics: Cap on metrics entering the prompt.
        max_dimensions: Cap on dimensions entering the prompt.
        min_relevance: Score an entity must reach to enter the prompt.
    """

    def __init__(
        self,
        *,
        run_context: RunContext,
        previous_followups: tuple[str, ...] = (),
        deliver: FollowUpCallback | None = None,
        timeout_sec: float = TIMEOUT_SEC,
        max_questions: int = MAX_QUESTIONS,
        max_metrics: int = MAX_METRICS,
        max_dimensions: int = MAX_DIMENSIONS,
        min_relevance: float = MIN_RELEVANCE,
    ) -> None:
        self.run_context = run_context
        self.chat_id = run_context.chat_id
        self.session_id = run_context.session_id
        self.previous_followups = previous_followups
        self.deliver = deliver
        self.timeout_sec = timeout_sec
        self.max_questions = max_questions
        self.max_metrics = max_metrics
        self.max_dimensions = max_dimensions
        self.min_relevance = min_relevance
        self._collector: SignalCollector | None = None
        self._snapshot: asyncio.Task[SignalSnapshot] | None = None
        self._pipeline: asyncio.Task[list[Candidate]] | None = None
        self._late: asyncio.Task[None] | None = None
        self._questions: list[str] | None = None

    async def start(self) -> None:
        """Spin up the collector; never raises."""
        if self._collector is not None:
            return
        try:
            collector = SignalCollector(
                previous_followups=self.previous_followups,
                max_metrics=self.max_metrics,
                max_dimensions=self.max_dimensions,
                min_relevance=self.min_relevance,
            )
            collector.start()
            self._collector = collector
        except Exception:
            logger.exception(
                "Follow-up recommendation failed to start for chat %s", self.chat_id
            )

    async def append(self, entry: dict[str, Any] | None, *, last: bool = False) -> None:
        """Enqueue one raw entry; ``last`` is the EOF sentinel and carries none.

        Non-blocking by contract: the queue is bounded and drops rather than
        waits, so a slow parse can never hold the reply up.
        """

        try:
            collector = self._collector
            if collector is not None and entry is not None:
                collector.submit(dict(entry))
            if last:
                # Freeze as soon as the stream ends, so a Chat that never asks
                # for a recommendation still leaves nothing running.
                self._begin_freeze()
        except Exception:
            logger.exception("Follow-up recommendation failed to accept an entry")

    async def join(self) -> list[str]:
        """Return the questions to recommend, or nothing if none can be made."""
        if self._questions is None:
            self._questions = await self._within_budget()
        return self._questions

    def _begin_freeze(self) -> None:
        if self._snapshot is None and self._collector is not None:
            self._snapshot = asyncio.create_task(self._freeze(self._collector))

    async def _freeze(self, collector: SignalCollector) -> SignalSnapshot:
        """Freezing runs whether or not anyone waits for it, so it cannot fail.

        A Chat that ends without asking for a recommendation still has to stop
        the collector, and there is nobody left to hand an error to.
        """

        try:
            return await collector.freeze()
        except Exception:
            logger.exception(
                "Follow-up signals failed to freeze for chat %s", self.chat_id
            )
            return SignalSnapshot()

    def _begin_pipeline(self) -> asyncio.Task[list[Candidate]] | None:
        if self._pipeline is None:
            self._begin_freeze()
            if self._snapshot is None:
                return None
            self._pipeline = asyncio.create_task(self._run_pipeline(self._snapshot))
        return self._pipeline

    async def _within_budget(self) -> list[str]:
        pipeline = self._begin_pipeline()
        if pipeline is None:
            return []
        try:
            # Shielded: the questions are worth persisting even once the turn
            # has stopped waiting for them.
            candidates = await asyncio.wait_for(
                asyncio.shield(pipeline), timeout=self.timeout_sec
            )
        except TimeoutError:
            logger.warning(
                "Follow-up recommendation missed its %ss budget for chat %s",
                self.timeout_sec,
                self.chat_id,
            )
            self._begin_late_delivery(pipeline)
            return []
        return FollowUp.of(self.chat_id, candidates).questions

    async def _run_pipeline(
        self, snapshot: asyncio.Task[SignalSnapshot]
    ) -> list[Candidate]:
        service = FollowUpService(
            timeout_sec=self.timeout_sec,
            max_questions=self.max_questions,
            llm=self._build_llm(),
        )
        try:
            return await service.recommend(await snapshot)
        except Exception:
            logger.exception(
                "Follow-up recommendation failed for chat %s", self.chat_id
            )
            return []

    def _build_llm(self) -> FollowUpLLM | None:
        """The model channel's model, or None to fall back to rules only.

        ``light`` is the small model the user picked for background work; the
        default model stands in when none is configured. Without either, a
        fresh env-configured model is built — never the agent's own instance,
        which retuning would corrupt.
        """

        config = self.run_context.user_runtime_config
        active = None
        if config is not None:
            active = getattr(config, "light", None) or getattr(
                config, "default", None
            )
        try:
            if active is not None:
                model = for_structured_calls(build_model(active))
            else:
                model = for_structured_calls(build_model_from_env())
        except Exception:
            logger.exception(
                "Follow-up has no usable model for chat %s; using rules only",
                self.chat_id,
            )
            return None
        return FollowUpLLM(model, timeout=self.timeout_sec)

    def _begin_late_delivery(self, pipeline: asyncio.Task[list[Candidate]]) -> None:
        if self.deliver is not None and self._late is None:
            self._late = asyncio.create_task(self._deliver_late(pipeline))

    async def _deliver_late(self, pipeline: asyncio.Task[list[Candidate]]) -> None:
        """Persist a result the turn outran, for the next snapshot to carry."""
        deliver = self.deliver
        if deliver is None:
            return
        try:
            questions = FollowUp.of(self.chat_id, await pipeline).questions
            if questions:
                await deliver(questions)
        except Exception:
            logger.exception(
                "Follow-up recommendation failed to deliver late for chat %s",
                self.chat_id,
            )


__all__ = ["FollowUpCallback", "FollowUpRecommend"]
