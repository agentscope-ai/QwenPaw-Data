# -*- coding: utf-8 -*-
from __future__ import annotations

from qwenpaw_data.host.core.api.models.artifact import ArtifactCommentSchema
from qwenpaw_data.host.core.api.models.chat import (
    AskUserQuestionAnsweredResultSchema,
    AskUserQuestionTimeoutResultSchema,
)
from qwenpaw_data.host.core.api.models.common import ApiModel, AttachmentRefSchema


class CreateSessionRequest(ApiModel):
    title: str | None = None
    datasource_id: str | None = None
    agent_id: str = "default"


class PatchSessionRequest(ApiModel):
    title: str | None = None


class CreateChatRequest(ApiModel):
    text: str
    datasource_id: str | None = None


class ConsoleChatRequest(ApiModel):
    session_id: str
    text: str
    datasource_id: str | None = None
    attachment_ids: list[str] = []
    attachments: list[AttachmentRefSchema] | None = None
    artifact_comments: list[ArtifactCommentSchema] = []


class SteerRequest(ApiModel):
    text: str


class ClarificationAnswerRequest(ApiModel):
    clarification_id: str
    result: AskUserQuestionAnsweredResultSchema | AskUserQuestionTimeoutResultSchema


class SettlementConfirmRequest(ApiModel):
    fields: dict[str, str] | None = None
