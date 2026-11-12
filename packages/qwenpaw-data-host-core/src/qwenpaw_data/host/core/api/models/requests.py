# -*- coding: utf-8 -*-
from __future__ import annotations

from typing import Literal

from pydantic import Field, field_validator

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
from qwenpaw_data.host.core.api.models.runtime_models import RuntimeModels
from qwenpaw_data.host.core.domain.session import ShareScope

MAX_DATASET_NAMES = 64
MAX_DATASET_NAME_LENGTH = 256


def normalize_dataset_names(names: list[str] | None) -> list[str] | None:
    """Chat admission rule kept from 0.3: nonblank, bounded, de-duplicated names."""
    if names is None:
        return None
    if any(not name.strip() or len(name) > MAX_DATASET_NAME_LENGTH for name in names):
        raise ValueError("dataset names must be nonblank and at most 256 characters")
    return list(dict.fromkeys(name.strip() for name in names))


class ChatAdmissionFields(ApiModel):
    """Fields shared by every chat-admission route (native console and 0.3 compat).

    ``runtime_models`` is the 0.3 / #81 request-scoped model binding. It is
    validated but never serialized, logged or persisted; handlers route it to
    the single ``model_binding_request`` launch parameter.
    """

    text: str
    datasource_id: str | None = None
    dataset_names: list[str] | None = Field(default=None, max_length=MAX_DATASET_NAMES)
    runtime_models: RuntimeModels | None = Field(
        default=None, exclude=True, repr=False
    )

    @field_validator("dataset_names")
    @classmethod
    def _valid_dataset_names(cls, names: list[str] | None) -> list[str] | None:
        return normalize_dataset_names(names)


class CreateSessionRequest(ApiModel):
    title: str | None = None
    datasource_id: str | None = None
    agent_id: str = "default"
    parent_session_id: str | None = None
    enable_clarification: bool = True


class PatchSessionRequest(ApiModel):
    title: str | None = None
    share_scope: ShareScope | None = None
    enable_clarification: bool | None = None


class ForkSessionRequest(ApiModel):
    chat_id: str


class ConsoleChatRequest(ChatAdmissionFields):
    session_id: str
    attachment_ids: list[str] = []
    attachments: list[AttachmentRefSchema] | None = None
    artifact_comments: list[ArtifactCommentSchema] = []
    # 0.3 clients assert clarification support; per-session control is
    # ``Session.enable_clarification`` (PATCH /sessions/{id}).
    enable_clarification: Literal[True] | None = Field(default=None, exclude=True)


class CreateChatRequest(ChatAdmissionFields):
    """0.3 ``POST /sessions/{session_id}/chats`` body used by the App bridge."""


class SteerRequest(ApiModel):
    text: str
    artifact_comments: list[ArtifactCommentSchema] = []


class ClarificationAnswerRequest(ApiModel):
    clarification_id: str
    result: AskUserQuestionAnsweredResultSchema | AskUserQuestionTimeoutResultSchema


class ToolConfirmationAnswerRequest(ApiModel):
    call_id: str
    decision: Literal["approved", "denied"]


class PlanEditRequest(ApiModel):
    reason: str | None = None
    plan: PlanSchema | None


class FeedbackRequest(ApiModel):
    kind: Literal["like", "dislike", "copy", "artifact_comment"]
    reason: str | None = None
    detail: str | None = None
    artifact_ref: ArtifactLineRefSchema | None = None


class SettlementConfirmRequest(ApiModel):
    fields: dict[str, str] | None = None


class CreateSkillDraftRequest(ApiModel):
    source_chat_id: str


class SkillDraftReviseRequest(ApiModel):
    text: str


class SkillProposalAcceptRequest(ApiModel):
    # Whole files to accept (every edit in those files). Ignored if hunk_ids is set.
    patch_ids: list[str] | None = None
    # Single edits to accept. If both are empty, accept every edit.
    hunk_ids: list[str] | None = None
