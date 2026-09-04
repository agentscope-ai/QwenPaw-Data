# -*- coding: utf-8 -*-
from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from qwenpaw_data.host.core.api.models.artifact import ArtifactCommentSchema
from qwenpaw_data.host.core.api.models.common import ApiModel, AttachmentRefSchema


class ChatErrorSchema(ApiModel):
    code: str
    message: str


class AskUserQuestionAnswerSchema(ApiModel):
    question: str
    selected_options: list[str]
    custom_text: str | None = None


class AskUserQuestionAnsweredResultSchema(ApiModel):
    status: Literal["answered"]
    answers: list[AskUserQuestionAnswerSchema]


class AskUserQuestionTimeoutResultSchema(ApiModel):
    status: Literal["timeout"]
    reason: str


class SessionSchema(ApiModel):
    id: str
    agent_id: str
    title: str
    status: Literal["idle", "running"]
    datasource_id: str | None = None
    chat_count: int = 0
    channel: str = "console"
    parent_session_id: str | None = None
    forked_from_chat_id: str | None = None
    created_at: datetime
    updated_at: datetime


class FollowUpSchema(ApiModel):
    chat_id: str | None = None
    questions: list[str]


class ChatSchema(ApiModel):
    id: str
    session_id: str
    sequence: int
    user_input: str
    datasource_id: str | None = None
    kind: Literal["simple", "planned"] = "simple"
    status: Literal["created", "running", "completed", "failed", "canceled"]
    last_sequence_number: int = -1
    started_at: datetime | None = None
    completed_at: datetime | None = None
    active_duration_ms: int = 0
    error: ChatErrorSchema | None = None
    plan: dict[str, Any] | None = None
    artifact_comments: list[ArtifactCommentSchema] = []
    attachments: list[AttachmentRefSchema] = []
