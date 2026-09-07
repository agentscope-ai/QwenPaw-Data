# -*- coding: utf-8 -*-
"""The algorithm side of the FollowUp contract: one recommender per Chat.

Everything the host sees is here: the constructor the host calls and the three
lifecycle calls. ``join`` hands back the questions rather than emitting them, so
the host keeps ownership of the stream and its ordering.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any

from qwenpaw_data.host.core.algo.followup.collector import SignalCollector
from qwenpaw_data.host.core.algo.followup.llm import FollowUpLLM, for_structured_calls
from qwenpaw_data.host.core.algo.followup.models import FollowUp, SignalSnapshot
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


class FollowUpRecommend:
    """Recommend the next questions for one Chat, out of the reply's way.

    The host drives ``start`` once, ``append`` per event, and ``join`` on the
    completed path only. Collection runs concurrently with the reply. At join,
    the service bounds the model call and falls back to rules before returning.

    Every knob is a plain argument with the validated default: the host owns the
    configuration (``settings.followup``) and passes it in, so nothing here
    reads the environment on its own. Whether recommendation runs at all is a
    host decision; this class is only constructed when the host wants it.

    Args:
        run_context: The Chat being answered, and the models it may run on.
        previous_followups: Questions recommended earlier in this Session, so
            the same one is not offered twice.
        timeout_sec: Maximum time allowed for the model channel.
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
        self.timeout_sec = timeout_sec
        self.max_questions = max_questions
        self.max_metrics = max_metrics
        self.max_dimensions = max_dimensions
        self.min_relevance = min_relevance
        self._collector: SignalCollector | None = None
        self._snapshot: asyncio.Task[SignalSnapshot] | None = None
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
            self._questions = await self._recommend()
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

    async def _recommend(self) -> list[str]:
        self._begin_freeze()
        if self._snapshot is None:
            return []
        service = FollowUpService(
            timeout_sec=self.timeout_sec,
            max_questions=self.max_questions,
            llm=self._build_llm(),
        )
        try:
            candidates = await service.recommend(await self._snapshot)
        except Exception:
            logger.exception(
                "Follow-up recommendation failed for chat %s", self.chat_id
            )
            return []
        return FollowUp.of(self.chat_id, candidates).questions

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


__all__ = ["FollowUpRecommend"]
