# -*- coding: utf-8 -*-
from __future__ import annotations

from datetime import datetime
from typing import Literal

from sqlalchemy import (
    Boolean,
    Float,
    Index,
    Integer,
    String,
    Text,
    PrimaryKeyConstraint,
    text,
    true,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.types import JSON

from qwenpaw_data.host.core.db.types import TZDateTime
from qwenpaw_data.host.core.utils.time import utcnow

SettlementCardStatus = Literal["pending", "confirmed", "dismissed", "queried"]
SkillProposalStatus = Literal["pending", "accepted", "partial", "rejected"]
SkillDraftStatus = Literal[
    "generating", "ready", "revising", "accepted", "dismissed", "failed"
]
SkillRevisionOrigin = Literal[
    "create",
    "install",
    "import",
    "manual_edit",
    "evolution_accept",
    "revert",
]
ModelUsageType = Literal["chat", "embedding"]


class Base(DeclarativeBase):
    type_annotation_map = {
        datetime: TZDateTime(),
    }


class IdentityColumns:
    """Mixin: tenant / workspace / user stamp on every business row."""

    user_id: Mapped[str] = mapped_column(String, nullable=False, index=True)
    tenant_id: Mapped[str] = mapped_column(String, nullable=False, index=True)
    workspace_id: Mapped[str] = mapped_column(String, nullable=False, index=True)


class SessionRow(IdentityColumns, Base):
    __tablename__ = "sessions"
    __table_args__ = (
        Index(
            "ix_sessions_catalog",
            "user_id",
            "tenant_id",
            "workspace_id",
            "updated_at",
            postgresql_where=text("deleted_at IS NULL"),
        ),
    )

    id: Mapped[str] = mapped_column(String, primary_key=True)
    agent_id: Mapped[str] = mapped_column(String, nullable=False, default="default")
    title: Mapped[str] = mapped_column(String, nullable=False, default="")
    datasource_id: Mapped[str | None] = mapped_column(String, nullable=True)
    channel: Mapped[str] = mapped_column(String, nullable=False, default="console")
    chat_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    parent_session_id: Mapped[str | None] = mapped_column(String, nullable=True)
    forked_from_chat_id: Mapped[str | None] = mapped_column(String, nullable=True)
    share_scope: Mapped[str] = mapped_column(
        String, nullable=False, default="none", server_default="none"
    )
    enable_clarification: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=true()
    )
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(default=utcnow)
    deleted_at: Mapped[datetime | None] = mapped_column(nullable=True)


class ChannelBindingRow(IdentityColumns, Base):
    """active_session_id → active session pointer, plus the send address recorded inbound.

    Keyed by ``(identity, channel, external_key)`` (composite PK via ``__table_args__``)
    so the cron-target picker can filter per user/tenant. ``target_type`` /
    ``send_meta`` / ``display_name`` fold in what the removed ``channel_targets`` table
    used to store, so a cron job created later can read ``send_meta`` without a separate
    lookup table.
    """

    __tablename__ = "channel_bindings"
    __table_args__ = (
        PrimaryKeyConstraint(
            "tenant_id", "user_id", "workspace_id", "channel", "external_key"
        ),
    )

    channel: Mapped[str] = mapped_column(String)
    external_key: Mapped[str] = mapped_column(String)
    active_session_id: Mapped[str] = mapped_column(String, nullable=False, index=True)
    target_type: Mapped[str] = mapped_column(String, nullable=False, default="")
    display_name: Mapped[str] = mapped_column(String, nullable=False, default="")
    send_meta: Mapped[dict] = mapped_column(JSON, nullable=False, default=lambda: dict)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(default=utcnow)


class ChatRow(IdentityColumns, Base):
    __tablename__ = "chats"
    __table_args__ = (
        Index("ix_chats_session_status", "session_id", "status"),
        Index(
            "ux_chats_session_running",
            "session_id",
            unique=True,
            sqlite_where=text("status = 'running'"),
            postgresql_where=text("status = 'running'"),
        ),
    )

    id: Mapped[str] = mapped_column(String, primary_key=True)
    session_id: Mapped[str] = mapped_column(String, nullable=False)
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    user_input: Mapped[str] = mapped_column(Text, nullable=False, default="")
    datasource_id: Mapped[str | None] = mapped_column(String, nullable=True)
    dataset_names: Mapped[list | None] = mapped_column(JSON, nullable=True)
    kind: Mapped[str] = mapped_column(String, nullable=False, default="simple")
    status: Mapped[str] = mapped_column(String, nullable=False, default="created")
    last_sequence_number: Mapped[int] = mapped_column(
        Integer, nullable=False, default=-1
    )
    started_at: Mapped[datetime | None] = mapped_column(nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(nullable=True)
    active_duration_ms: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    error_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    plan: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    artifact_comments_json: Mapped[list] = mapped_column(
        JSON, nullable=False, default=list
    )
    attachments_json: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    agent_state_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    enable_clarification: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=true()
    )
    heartbeat_at: Mapped[datetime | None] = mapped_column(nullable=True)
    otel_span_id: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(default=utcnow)


class ChatCommandRow(IdentityColumns, Base):
    __tablename__ = "chat_commands"
    __table_args__ = (Index("ix_chat_commands_chat_kind", "chat_id", "kind", "status"),)

    id: Mapped[str] = mapped_column(String, primary_key=True)
    chat_id: Mapped[str] = mapped_column(String, nullable=False)
    kind: Mapped[
        Literal["cancel", "steer", "clarification", "plan", "tool_confirmation", "ui"]
    ] = mapped_column(String, nullable=False)
    status: Mapped[Literal["waiting", "done"]] = mapped_column(
        String, nullable=False, default="waiting"
    )
    steer_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    clarification_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    plan_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    tool_confirmation_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    ui_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)


class ChatEventRow(IdentityColumns, Base):
    __tablename__ = "chat_events"
    __table_args__ = (
        Index(
            "ix_chat_events_chat_object_seq",
            "chat_id",
            "object",
            "sequence_number",
        ),
    )

    chat_id: Mapped[str] = mapped_column(String, primary_key=True)
    sequence_number: Mapped[int] = mapped_column(Integer, primary_key=True)
    object: Mapped[str] = mapped_column(String, nullable=False)
    session_id: Mapped[str] = mapped_column(String, nullable=False)
    payload: Mapped[dict] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)


class ModelUsageRow(IdentityColumns, Base):
    __tablename__ = "model_usages"
    __table_args__ = (
        Index(
            "ix_model_usages_scope",
            "tenant_id",
            "workspace_id",
            "user_id",
            "created_at",
        ),
    )

    id: Mapped[str] = mapped_column(String, primary_key=True)
    session_id: Mapped[str] = mapped_column(String, nullable=False)
    chat_id: Mapped[str] = mapped_column(String, nullable=False)
    source: Mapped[str] = mapped_column(String, nullable=False)
    source_step: Mapped[str] = mapped_column(String, nullable=False)
    model_type: Mapped[ModelUsageType] = mapped_column(String, nullable=False)
    provider_id: Mapped[str] = mapped_column(String, nullable=False)
    model_id: Mapped[str] = mapped_column(String, nullable=False)
    input_tokens: Mapped[int] = mapped_column(Integer, nullable=False)
    output_tokens: Mapped[int] = mapped_column(Integer, nullable=False)
    cache_read_tokens: Mapped[int] = mapped_column(Integer, nullable=False)
    cache_creation_tokens: Mapped[int] = mapped_column(Integer, nullable=False)
    duration_ms: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)


class SegmentRow(IdentityColumns, Base):
    __tablename__ = "segments"

    chat_id: Mapped[str] = mapped_column(String, primary_key=True)
    id: Mapped[str] = mapped_column(String, primary_key=True)
    title: Mapped[str] = mapped_column(String, nullable=False)
    input: Mapped[str | None] = mapped_column(Text, nullable=True)
    behavior: Mapped[str] = mapped_column(Text, nullable=False, default="")
    conclusion: Mapped[str] = mapped_column(Text, nullable=False, default="")
    artifact_json: Mapped[list | None] = mapped_column(JSON, nullable=True)
    confidence_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    start_seq: Mapped[int] = mapped_column(Integer, nullable=False)
    end_seq: Mapped[int] = mapped_column(Integer, nullable=False)
    started_at: Mapped[float | None] = mapped_column(Float, nullable=True)
    ended_at: Mapped[float | None] = mapped_column(Float, nullable=True)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)


class BizEventRow(IdentityColumns, Base):
    __tablename__ = "biz_events"

    chat_id: Mapped[str] = mapped_column(String, primary_key=True)
    event_id: Mapped[str] = mapped_column(String, primary_key=True)
    seq: Mapped[int] = mapped_column(Integer, nullable=False)
    channel: Mapped[str] = mapped_column(String, nullable=False, default="main")
    block_id: Mapped[str | None] = mapped_column(String, nullable=True)
    status: Mapped[str] = mapped_column(String, nullable=False, default="done")
    presentation_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    started_at: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    ended_at: Mapped[float | None] = mapped_column(Float, nullable=True)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)


class ArtifactRow(IdentityColumns, Base):
    __tablename__ = "artifacts"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    session_id: Mapped[str] = mapped_column(String, nullable=False, index=True)
    chat_id: Mapped[str | None] = mapped_column(String, nullable=True, index=True)
    name: Mapped[str] = mapped_column(String, nullable=False)
    path: Mapped[str] = mapped_column(String, nullable=False)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(default=utcnow)


class SettlementCardRow(IdentityColumns, Base):
    __tablename__ = "settlement_cards"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    session_id: Mapped[str] = mapped_column(String, nullable=False, index=True)
    source_chat_id: Mapped[str] = mapped_column(String, nullable=False)
    type: Mapped[str] = mapped_column(String, nullable=False)
    fields_json: Mapped[dict] = mapped_column(JSON, nullable=False)
    status: Mapped[SettlementCardStatus] = mapped_column(
        String, nullable=False, default="pending"
    )
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(default=utcnow)
    confirmed_at: Mapped[datetime | None] = mapped_column(nullable=True)


class SkillProposalRow(IdentityColumns, Base):
    __tablename__ = "skill_proposals"
    __table_args__ = (
        Index("ix_skill_proposals_session_status", "session_id", "status"),
    )

    id: Mapped[str] = mapped_column(String, primary_key=True)
    session_id: Mapped[str] = mapped_column(String, nullable=False, index=True)
    agent_id: Mapped[str] = mapped_column(String, nullable=False)
    source_chat_id: Mapped[str | None] = mapped_column(String, nullable=True)
    status: Mapped[SkillProposalStatus] = mapped_column(
        String, nullable=False, default="pending"
    )
    summary: Mapped[str] = mapped_column(Text, nullable=False)
    mentioned_skill_names: Mapped[list] = mapped_column(JSON, nullable=False)
    modified_file_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # Proposed skill file edits
    file_changes: Mapped[list] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(default=utcnow)


class SkillDraftRow(IdentityColumns, Base):
    __tablename__ = "skill_drafts"
    __table_args__ = (Index("ix_skill_drafts_session_status", "session_id", "status"),)

    id: Mapped[str] = mapped_column(String, primary_key=True)
    session_id: Mapped[str] = mapped_column(String, nullable=False, index=True)
    source_chat_id: Mapped[str] = mapped_column(String, nullable=False)
    status: Mapped[SkillDraftStatus] = mapped_column(
        String, nullable=False, default="generating"
    )
    files_json: Mapped[dict] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(default=utcnow)


class SkillRevisionRow(IdentityColumns, Base):
    """Append-only history of one agent skill directory."""

    __tablename__ = "skill_revisions"
    __table_args__ = (
        Index(
            "ix_skill_revisions_agent_skill_rev",
            "agent_id",
            "skill_name",
            "revision_no",
        ),
    )

    id: Mapped[str] = mapped_column(String, primary_key=True)
    agent_id: Mapped[str] = mapped_column(String, nullable=False)
    skill_name: Mapped[str] = mapped_column(String, nullable=False)
    revision_no: Mapped[int] = mapped_column(Integer, nullable=False)
    origin: Mapped[SkillRevisionOrigin] = mapped_column(String, nullable=False)
    # Proposal id / session id / reverted revision, depending on origin.
    source_ref: Mapped[str] = mapped_column(String, nullable=False, default="")
    note: Mapped[str] = mapped_column(Text, nullable=False, default="")
    # Full snapshot of the skill directory: [{relative_path, content}]
    files: Mapped[list] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)


class AgentSkillStateRow(IdentityColumns, Base):
    __tablename__ = "agent_skill_states"
    __table_args__ = (
        PrimaryKeyConstraint(
            "user_id",
            "tenant_id",
            "workspace_id",
            "agent_id",
            "skill_name",
        ),
    )

    agent_id: Mapped[str] = mapped_column(String, nullable=False)
    skill_name: Mapped[str] = mapped_column(String, nullable=False)
    disabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(default=utcnow)


class CronJobRow(IdentityColumns, Base):
    __tablename__ = "cron_jobs"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    name: Mapped[str] = mapped_column(String, nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    message: Mapped[str] = mapped_column(Text, nullable=False, default="")
    datasource_id: Mapped[str] = mapped_column(String, nullable=False)
    channel: Mapped[str] = mapped_column(String, nullable=False, default="console")
    session_policy: Mapped[str] = mapped_column(
        String, nullable=False, default="reuse", server_default="reuse"
    )
    session_id: Mapped[str | None] = mapped_column(String, nullable=True)
    target_external_key: Mapped[str | None] = mapped_column(String, nullable=True)
    schedule_json: Mapped[dict] = mapped_column(JSON, nullable=False)
    next_run_at: Mapped[datetime | None] = mapped_column(nullable=True)
    running_at: Mapped[datetime | None] = mapped_column(nullable=True)
    # 这次执行期间，占着它的机器按间隔写成当前时间。
    heartbeat_at: Mapped[datetime | None] = mapped_column(nullable=True)
    source_chat_id: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(default=utcnow)


class FollowUpRow(IdentityColumns, Base):
    __tablename__ = "followups"

    chat_id: Mapped[str] = mapped_column(String, primary_key=True)
    questions_json: Mapped[list] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)


class AttachmentRow(IdentityColumns, Base):
    __tablename__ = "attachments"
    __table_args__ = (
        Index("ix_attachments_session_filename", "session_id", "filename"),
    )

    attachment_id: Mapped[str] = mapped_column(String, primary_key=True)
    session_id: Mapped[str] = mapped_column(String, nullable=False)
    filename: Mapped[str] = mapped_column(String, nullable=False)
    storage_path: Mapped[str] = mapped_column(String, nullable=False)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)


class FeedbackRow(IdentityColumns, Base):
    __tablename__ = "feedbacks"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    session_id: Mapped[str] = mapped_column(String, nullable=False)
    chat_id: Mapped[str] = mapped_column(String, nullable=False, index=True)
    kind: Mapped[str] = mapped_column(String, nullable=False)
    reason: Mapped[str | None] = mapped_column(String, nullable=True)
    detail: Mapped[str | None] = mapped_column(Text, nullable=True)
    artifact_ref_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)


class BehaviorEventRow(IdentityColumns, Base):
    __tablename__ = "behavior_events"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    session_id: Mapped[str] = mapped_column(String, nullable=False, index=True)
    chat_id: Mapped[str | None] = mapped_column(String, nullable=True, index=True)
    kind: Mapped[str] = mapped_column(String, nullable=False)
    target_type: Mapped[str] = mapped_column(String, nullable=False, default="")
    target_id: Mapped[str] = mapped_column(String, nullable=False, default="")
    payload_json: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)


class ScoreDeliveryRow(IdentityColumns, Base):
    """One outbound score. ``sent_at`` stops a second POST on Langfuse v4."""

    __tablename__ = "score_deliveries"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    chat_id: Mapped[str] = mapped_column(String, nullable=False, index=True)
    session_id: Mapped[str | None] = mapped_column(String, nullable=True, index=True)
    name: Mapped[str] = mapped_column(String, nullable=False)
    data_type: Mapped[str] = mapped_column(String, nullable=False)
    value_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    value_num: Mapped[float | None] = mapped_column(Float, nullable=True)
    comment: Mapped[str | None] = mapped_column(Text, nullable=True)
    metadata_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    op: Mapped[str] = mapped_column(String, nullable=False, default="create")
    sent_at: Mapped[datetime | None] = mapped_column(nullable=True)
    attempts: Mapped[int] = mapped_column(nullable=False, default=0, server_default="0")
    created_at: Mapped[datetime] = mapped_column(default=utcnow)


class SessionAgentStateRow(IdentityColumns, Base):
    __tablename__ = "session_agent_states"

    session_id: Mapped[str] = mapped_column(String, primary_key=True)
    state_json: Mapped[dict] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(default=utcnow)


class ChannelRow(IdentityColumns, Base):
    """channel_config.py: class ChannelConfigPayload(ApiModel):
    channel_config_store.py: _initial_config()
    """

    __tablename__ = "channels"

    config_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)

    __table_args__ = (PrimaryKeyConstraint("tenant_id", "user_id", "workspace_id"),)


class UserProviderRow(Base):
    __tablename__ = "user_providers"

    user_id: Mapped[str] = mapped_column(String, primary_key=True)
    provider_id: Mapped[str] = mapped_column(String, primary_key=True)
    api_key_enc: Mapped[str] = mapped_column(Text, nullable=False)
    base_url: Mapped[str | None] = mapped_column(String, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(default=utcnow)


class UserProviderModelRow(Base):
    __tablename__ = "user_provider_models"

    user_id: Mapped[str] = mapped_column(String, primary_key=True)
    provider_id: Mapped[str] = mapped_column(String, primary_key=True)
    model_id: Mapped[str] = mapped_column(String, primary_key=True)
    source: Mapped[str] = mapped_column(String, nullable=False)
    name: Mapped[str | None] = mapped_column(String, nullable=True)
    context_size: Mapped[int | None] = mapped_column(Integer, nullable=True)
    thinking_enabled: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    generate_kwargs_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(default=utcnow)


class UserActiveModelRow(Base):
    __tablename__ = "user_active_models"

    user_id: Mapped[str] = mapped_column(String, primary_key=True)
    default_provider_id: Mapped[str] = mapped_column(String, nullable=False)
    default_model_id: Mapped[str] = mapped_column(String, nullable=False)
    light_provider_id: Mapped[str | None] = mapped_column(String, nullable=True)
    light_model_id: Mapped[str | None] = mapped_column(String, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(default=utcnow)


class UserRuntimeSettingsRow(Base):
    __tablename__ = "user_runtime_settings"

    user_id: Mapped[str] = mapped_column(String, primary_key=True)
    react_max_iters: Mapped[int | None] = mapped_column(Integer, nullable=True)
    llm_retry_enabled: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    llm_max_retries: Mapped[int | None] = mapped_column(Integer, nullable=True)
    context_trigger_ratio: Mapped[float | None] = mapped_column(Float, nullable=True)
    context_reserve_ratio: Mapped[float | None] = mapped_column(Float, nullable=True)
    tool_result_limit: Mapped[int | None] = mapped_column(Integer, nullable=True)
    tool_timeout_enabled: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    tool_timeout_seconds: Mapped[float | None] = mapped_column(Float, nullable=True)
    tool_confirmation_level: Mapped[str | None] = mapped_column(String, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(default=utcnow)
