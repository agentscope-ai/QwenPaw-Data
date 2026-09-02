# -*- coding: utf-8 -*-
from __future__ import annotations

from qwenpaw_data.host.core.api.models.chat import (
    AskUserQuestionAnsweredResultSchema,
    AskUserQuestionTimeoutResultSchema,
)
from qwenpaw_data.host.core.api.models.common import ApiModel


class CreateSessionRequest(ApiModel):
    title: str | None = None
    datasource_id: str | None = None
    agent_id: str = "default"


class PatchSessionRequest(ApiModel):
    title: str | None = None


class CreateChatRequest(ApiModel):
    text: str
    datasource_id: str | None = None


class SteerRequest(ApiModel):
    text: str


class ClarificationAnswerRequest(ApiModel):
    clarification_id: str
    result: AskUserQuestionAnsweredResultSchema | AskUserQuestionTimeoutResultSchema
