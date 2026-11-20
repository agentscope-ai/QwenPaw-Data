# -*- coding: utf-8 -*-
from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from qwenpaw_data.host.core.domain.identity import Identity


class ModelUsageSource(StrEnum):
    DATA_AGENT = "data-agent"
    REVIEW_AGENT = "review-agent"
    FOLLOWUP = "followup"
    SETTLEMENT = "settlement"
    BIZTRACE = "biztrace"
    SKILL_CREATE = "skill-create"
    SKILL_EVOLUTION = "skill-evolution"
    EVALUATION = "evaluation"
    COMPARE = "compare"


class ModelUsageType(StrEnum):
    CHAT = "chat"
    EMBEDDING = "embedding"


@dataclass(frozen=True)
class ModelUsageContext:
    source: ModelUsageSource
    identity: Identity
    session_id: str
    chat_id: str
    source_step: str | StrEnum = ""
    model_type: ModelUsageType = ModelUsageType.CHAT
