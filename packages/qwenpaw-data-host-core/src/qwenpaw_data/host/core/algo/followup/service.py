# -*- coding: utf-8 -*-
"""Prefer the model channel; fall back to rule templates only when it fails.

The model gets the full end-of-turn budget and is only safety-filtered
(:func:`ranking.finalize_model`), since the prompt already asks for 2~3
ordered questions. Rules run — with full scored :func:`ranking.select` — only
when that call is missing, times out, errors, or leaves too few survivors.
"""

from __future__ import annotations

import asyncio
import logging

from qwenpaw_data.host.core.algo.followup import ranking
from qwenpaw_data.host.core.algo.followup.generator import generate_llm_candidates
from qwenpaw_data.host.core.algo.followup.llm import FollowUpLLM
from qwenpaw_data.host.core.algo.followup.models import Candidate, SignalSnapshot
from qwenpaw_data.host.core.algo.followup.rules import generate_rule_candidates
from qwenpaw_data.host.core.algo.followup.settings import MAX_QUESTIONS, TIMEOUT_SEC

logger = logging.getLogger(__name__)


class FollowUpService:
    """Turn one frozen snapshot into the questions worth recommending.

    Args:
        timeout_sec: Budget for the model channel.
        max_questions: Upper bound on the delivered questions.
        llm: The model channel, or None to run on templates alone.
    """

    def __init__(
        self,
        *,
        timeout_sec: float = TIMEOUT_SEC,
        max_questions: int = MAX_QUESTIONS,
        llm: FollowUpLLM | None = None,
    ) -> None:
        self.timeout_sec = timeout_sec
        self.max_questions = max_questions
        self.llm = llm

    async def recommend(self, snapshot: SignalSnapshot) -> list[Candidate]:
        """Return model survivors, or rule templates if the model cannot."""
        llm_candidates: list[Candidate] = []
        try:
            llm_candidates = await asyncio.wait_for(
                generate_llm_candidates(snapshot, llm=self.llm),
                timeout=self.timeout_sec,
            )
        except TimeoutError:
            logger.warning(
                "Follow-up model channel timed out; falling back to rules"
            )

        if llm_candidates:
            # Prompt already sized/ordered the batch; only safety gates apply.
            picked = ranking.finalize_model(
                llm_candidates, snapshot, self.max_questions
            )
            if picked:
                return picked
            logger.warning(
                "Follow-up model candidates were all filtered; "
                "falling back to rules"
            )

        return ranking.select(
            generate_rule_candidates(snapshot), snapshot, self.max_questions
        )


__all__ = ["FollowUpService"]
