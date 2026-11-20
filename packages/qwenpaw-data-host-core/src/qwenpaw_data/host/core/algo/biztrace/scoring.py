# -*- coding: utf-8 -*-
"""Delimit-to-publish bookkeeping for segment confidence scoring."""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any

from qwenpaw_data.host.core.algo.biztrace.models import Segment

if TYPE_CHECKING:
    from qwenpaw_data.host.core.algo.evaluation.grader import SegmentGrader
    from qwenpaw_data.host.core.algo.evaluation.models import ConfidenceResult

logger = logging.getLogger(__name__)


class SegmentScoring:
    """Grade each delimited segment and attach the verdict before publish."""

    def __init__(
        self,
        grader: SegmentGrader | None,
    ) -> None:
        self.grader = grader
        self._tasks: dict[str, asyncio.Task[ConfidenceResult | None]] = {}

    async def start(
        self,
        segment: Segment,
        *,
        blocks: list[dict[str, Any]],
        plan: list[dict[str, Any]],
        subtask: str | None,
        completed: list[str],
        priors: list[tuple[int, str]],
    ) -> None:
        """Kick the grade off at delimit time; extraction runs in parallel."""
        if self.grader is None:
            return
        self._tasks[segment.segment_id] = asyncio.create_task(
            self.score(
                segment,
                blocks=blocks,
                plan=plan,
                subtask=subtask,
                completed=completed,
                priors=priors,
            )
        )

    async def join(self, segment: Segment, *, budget: float) -> None:
        """Wait out the in-flight score and attach it to the publish row.

        Only ``TimeoutError`` is absorbed: a slow grade publishes without a
        badge. ``CancelledError`` is left to the caller so an outer close
        can still distinguish a cancelled extraction.
        """
        task = self._tasks.pop(segment.segment_id, None)
        if task is None:
            return
        try:
            segment.confidence = await asyncio.wait_for(task, budget)
        except TimeoutError:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            logger.warning("confidence grade did not land for %s", segment.segment_id)

    async def discard(self, segment_id: str) -> None:
        """Drop a failed segment's in-flight score: nobody will join it."""
        task = self._tasks.pop(segment_id, None)
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def aclose(self) -> None:
        """Cancel every leftover score so nothing outlives the assembler."""
        tasks = list(self._tasks.values())
        self._tasks.clear()
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def score(
        self,
        segment: Segment,
        *,
        blocks: list[dict[str, Any]],
        plan: list[dict[str, Any]],
        subtask: str | None,
        completed: list[str],
        priors: list[tuple[int, str]],
    ) -> ConfidenceResult | None:
        """Grade one segment; every failure path returns None."""
        if self.grader is None:
            return None
        try:
            return await self.grader.grade(
                blocks=blocks,
                plan=plan,
                subtask=subtask,
                completed=completed,
                priors=priors,
            )
        except Exception:
            logger.exception("confidence scoring failed for %s", segment.segment_id)
            return None


__all__ = ["SegmentScoring"]
