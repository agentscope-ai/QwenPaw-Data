# -*- coding: utf-8 -*-
from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from qwenpaw_data.host.core.api.models.artifact import (
    ArtifactCommentSchema,
    ArtifactLineRefSchema,
    ArtifactSchema,
)
from qwenpaw_data.host.core.api.models.common import ApiModel, AttachmentRefSchema
from qwenpaw_data.host.core.api.models.plan import PlanSchema
from qwenpaw_data.host.core.api.models.trace import BizEventSchema, SegmentSchema
from qwenpaw_data.host.core.domain.session import ShareScope


class ChatErrorSchema(ApiModel):
    code: Literal[
        "MODEL_AUTH",
        "MODEL_QUOTA",
        "MODEL_RATE_LIMIT",
        "MODEL_NOT_FOUND",
        "CONTEXT_OVERFLOW",
        "REACT_MAX_ITERS",
        "MODEL_UNAVAILABLE",
        "CONFIG",
        "INTERNAL",
        "VALIDATION",
    ]
    message: str
    details: dict[str, Any] | None = None


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


class FeedbackSchema(ApiModel):
    id: str
    session_id: str
    chat_id: str
    kind: Literal["like", "dislike", "copy", "artifact_comment"]
    reason: str | None = None
    detail: str | None = None
    artifact_ref: ArtifactLineRefSchema | None = None
    created_at: datetime


class FollowUpSchema(ApiModel):
    chat_id: str | None = None
    questions: list[str]


class ChatSchema(ApiModel):
    id: str
    session_id: str
    sequence: int
    user_input: str
    datasource_id: str | None = None
    dataset_names: list[str] | None = None
    kind: Literal["simple", "planned"] = "simple"
    status: Literal["created", "running", "completed", "failed", "canceled"]
    forkable: bool = False
    last_sequence_number: int = -1
    started_at: datetime | None = None
    completed_at: datetime | None = None
    active_duration_ms: int = 0
    error: ChatErrorSchema | None = None
    plan: PlanSchema | None = None
    segments: list[SegmentSchema] = []
    biz_events: list[BizEventSchema] = []
    artifacts: list[ArtifactSchema] = []
    followup: FollowUpSchema | None = None
    artifact_comments: list[ArtifactCommentSchema] = []
    attachments: list[AttachmentRefSchema] = []
    enable_clarification: bool = True


class SessionSchema(ApiModel):
    id: str
    display_code: str = ""
    owner_user_id: str
    agent_id: str
    title: str
    status: Literal["idle", "running"]
    datasource_id: str | None = None
    chat_count: int = 0
    channel: str = "console"
    parent_session_id: str | None = None
    forked_from_chat_id: str | None = None
    share_scope: ShareScope = "none"
    enable_clarification: bool = True
    created_at: datetime
    updated_at: datetime
