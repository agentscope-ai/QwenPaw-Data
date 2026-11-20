# -*- coding: utf-8 -*-
"""Per-chat segment grading service."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from qwenpaw_data.host.core.algo.evaluation.evidence import build_evidence
from qwenpaw_data.host.core.algo.evaluation.models import ConfidenceResult
from qwenpaw_data.host.core.algo.evaluation.scorer import ConfidenceScorer
from qwenpaw_data.host.core.algo.evaluation.source import fetch_user_inputs
from qwenpaw_data.host.core.db.engine import get_session_factory
from qwenpaw_data.host.core.domain.model_usage import ModelUsageContext

if TYPE_CHECKING:
    from qwenpaw_data.host.core.domain.preference import UserRuntimeConfig

logger = logging.getLogger(__name__)


class SegmentGrader:
    """Per-chat grading service: fetch typed inputs, judge the span."""

    def __init__(
        self,
        config: UserRuntimeConfig | None,
        *,
        usage: ModelUsageContext,
    ) -> None:
        self._scorer = ConfidenceScorer(config, usage=usage)
        self._usage = usage
        self._user_inputs: list[str] | None = None

    async def grade(
        self,
        *,
        blocks: list[dict[str, Any]] | None = None,
        plan: list[dict[str, Any]] | None = None,
        subtask: str | None = None,
        completed: list[str] | None = None,
        priors: list[tuple[int, str]] | None = None,
    ) -> ConfidenceResult | None:
        """Grade one delimited span from already-projected materials."""
        evidence = build_evidence(
            blocks,
            plan=plan,
            subtask=subtask,
            completed=completed,
            priors=priors,
            user_inputs=await self._inputs(),
        )
        return await self._scorer.score(evidence)

    async def _inputs(self) -> list[str]:
        """Typed inputs are fixed at chat creation, so one read is enough."""
        if self._user_inputs is None:
            self._user_inputs = await fetch_user_inputs(
                get_session_factory(), self._usage.identity, self._usage.chat_id
            )
        return self._user_inputs


__all__ = ["SegmentGrader"]
