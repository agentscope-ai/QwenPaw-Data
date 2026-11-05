# -*- coding: utf-8 -*-
from __future__ import annotations

from typing import Literal
from pydantic import Field, field_validator
from .runtime_models import RuntimeModels

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


class ForkSessionRequest(ApiModel):
    chat_id: str


class CreateChatRequest(ApiModel):
    text: str
    datasource_id: str | None = None


class ConsoleChatRequest(ApiModel):
    runtime_models: RuntimeModels | None = Field(default=None, exclude=True, repr=False)
    dataset_names: list[str] | None = Field(default=None, max_length=64)
    enable_clarification: Literal[True] = True

    @field_validator("dataset_names")
    @classmethod
    def valid_dataset_names(cls, names):
        if names is not None and any(
            not name.strip() or len(name) > 256 for name in names
        ):
            raise ValueError(
                "dataset names must be nonblank and at most 256 characters"
            )
        return list(dict.fromkeys(name.strip() for name in names)) if names else []

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
