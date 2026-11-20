# -*- coding: utf-8 -*-
"""The confidence scorer: one structured call; the verdict passes through."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from agentscope.message import SystemMsg, UserMsg

from qwenpaw_data.host.core.algo.evaluation.models import ConfidenceResult
from qwenpaw_data.host.core.algo.evaluation.prompt import (
    build_confidence_user_prompt,
    get_confidence_system_prompt,
)
from qwenpaw_data.host.core.algo.evaluation.settings import (
    CONFIDENCE_TIMEOUT_SECONDS,
)
from qwenpaw_data.host.core.algo.settlement.llm import (
    StructuredCallError,
    for_structured_calls,
    structured_call,
)
from qwenpaw_data.host.core.domain.model_usage import ModelUsageContext
from qwenpaw_data.host.core.providers.factory import build_model

if TYPE_CHECKING:
    from qwenpaw_data.host.core.domain.preference import UserRuntimeConfig

logger = logging.getLogger(__name__)


def _build_model(config: UserRuntimeConfig | None, usage: ModelUsageContext) -> Any:
    """The user's light model; None keeps every segment unbadged."""
    if config is None:
        return None
    try:
        active = config.light or config.default
        return for_structured_calls(build_model(active, usage=usage))
    except Exception:
        logger.exception("confidence scoring has no usable model")
        return None


class ConfidenceScorer:
    """Judge one segment's trustworthiness from its session record."""

    def __init__(
        self, config: UserRuntimeConfig | None, *, usage: ModelUsageContext
    ) -> None:
        self.failures = 0
        self._model = _build_model(config, usage)

    async def score(self, record: str) -> ConfidenceResult | None:
        """Return the verdict, or None to publish without a badge."""
        if self._model is None:
            return None
        messages = [
            SystemMsg(
                name="system",
                content=get_confidence_system_prompt(),
            ),
            UserMsg(
                name="user",
                content=build_confidence_user_prompt(record),
            ),
        ]
        try:
            return await structured_call(  # type: ignore[return-value]
                self._model,
                messages,
                ConfidenceResult,
                timeout=CONFIDENCE_TIMEOUT_SECONDS,
            )
        except StructuredCallError as exc:
            self.failures += 1
            logger.warning("confidence scoring failed: %s", exc)
            return None


__all__ = ["ConfidenceScorer"]
