# -*- coding: utf-8 -*-
from __future__ import annotations

from typing import Literal

from qwenpaw_data.host.core.api.models.artifact import (
    ArtifactCommentSchema,
    ArtifactLineRefSchema,
)
from qwenpaw_data.host.core.api.models.chat import (
    AskUserQuestionAnsweredResultSchema,
    AskUserQuestionTimeoutResultSchema,
)
from qwenpaw_data.host.core.api.models.common import ApiModel, AttachmentRefSchema
from qwenpaw_data.host.core.api.models.plan import PlanSchema


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
    artifact_comments: list[ArtifactCommentSchema] = []


class PlanEditRequest(ApiModel):
    reason: str | None = None
    plan: PlanSchema | None


class FeedbackRequest(ApiModel):
    kind: Literal["like", "dislike", "copy", "artifact_comment"]
    reason: str | None = None
    detail: str | None = None
    artifact_ref: ArtifactLineRefSchema | None = None


class ClarificationAnswerRequest(ApiModel):
    clarification_id: str
    result: AskUserQuestionAnsweredResultSchema | AskUserQuestionTimeoutResultSchema


class SettlementConfirmRequest(ApiModel):
    fields: dict[str, str] | None = None
