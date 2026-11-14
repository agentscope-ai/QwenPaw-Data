# -*- coding: utf-8 -*-
from __future__ import annotations

from typing import Any

from agentscope.state import TaskContext

from qwenpaw_data.host.core.api.models.artifact import ArtifactCommentSchema, ArtifactSchema
from qwenpaw_data.host.core.api.models.common import AttachmentRefSchema
from qwenpaw_data.host.core.api.models.chat import (
    ChatErrorSchema,
    ChatSchema,
    FeedbackSchema,
    FollowUpSchema,
    SessionSchema,
)
from qwenpaw_data.host.core.api.models.plan import PlanSchema
from qwenpaw_data.host.core.api.models.preferences import ProviderModelSchema, ProviderSchema
from qwenpaw_data.host.core.api.models.trace import (
    BizEventSchema,
    MessageSchema,
    SegmentSchema,
)
from qwenpaw_data.host.core.domain.chat import Chat
from qwenpaw_data.host.core.domain.preference import UserPreferences
from qwenpaw_data.host.core.domain.session import Session
from qwenpaw_data.host.core.providers.registry import ProviderRegistry
from qwenpaw_data.host.core.repositories.snapshot import ChatBundle
from qwenpaw_data.host.core.settings import get_settings
from qwenpaw_data.host.core.utils.secrets import mask_api_key
from qwenpaw_data.host.core.utils.session_naming import session_display_code


def session_to_schema(session: Session, *, has_active_chat: bool) -> SessionSchema:
    return SessionSchema(
        id=session.id,
        display_code=session_display_code(session.id),
        owner_user_id=session.identity.user_id,
        agent_id=session.agent_id,
        title=session.title,
        status=session.derive_status(has_active_chat=has_active_chat),  # type: ignore[arg-type]
        datasource_id=session.datasource_id,
        chat_count=session.chat_count,
        channel=session.channel,
        parent_session_id=session.parent_session_id,
        forked_from_chat_id=session.forked_from_chat_id,
        share_scope=session.share_scope,
        enable_clarification=session.enable_clarification,
        created_at=session.created_at,
        updated_at=session.updated_at,
    )


def chat_to_schema(chat: Chat, bundle: ChatBundle | None = None) -> ChatSchema:
    err = ChatErrorSchema.model_validate(chat.error) if chat.error else None
    if bundle is None:
        segments: list[Any] = []
        biz_events: list[Any] = []
        artifacts: list[Any] = []
        followup = None
    else:
        segments = bundle.segments
        biz_events = bundle.biz_events
        artifacts = bundle.artifacts
        followup = (
            FollowUpSchema.model_validate(bundle.followup) if bundle.followup else None
        )
    return ChatSchema(
        id=chat.id,
        session_id=chat.session_id,
        sequence=chat.sequence,
        user_input=chat.user_input,
        datasource_id=chat.datasource_id,
        dataset_names=chat.dataset_names,
        kind=chat.kind,  # type: ignore[arg-type]
        status=chat.status,  # type: ignore[arg-type]
        forkable=chat.status == "completed" and bool(chat.agent_state),
        last_sequence_number=chat.last_sequence_number,
        started_at=chat.started_at,
        completed_at=chat.completed_at,
        active_duration_ms=chat.active_duration_ms,
        error=err,
        plan=plan_to_schema(chat.plan),
        segments=[SegmentSchema.model_validate(x) for x in segments],
        biz_events=[BizEventSchema.model_validate(x) for x in biz_events],
        artifacts=[ArtifactSchema.model_validate(x) for x in artifacts],
        followup=followup,
        artifact_comments=[
            ArtifactCommentSchema.model_validate(item)
            for item in chat.artifact_comments
        ],
        attachments=[
            AttachmentRefSchema.model_validate(item) for item in chat.attachments
        ],
        enable_clarification=chat.enable_clarification,
    )


def chat_bundle_to_schema(bundle: ChatBundle) -> ChatSchema:
    return chat_to_schema(bundle.chat, bundle)


def feedback_to_schema(data: dict[str, Any]) -> FeedbackSchema:
    return FeedbackSchema.model_validate(data)


def message_to_schema(data: dict[str, Any]) -> MessageSchema:
    return MessageSchema.model_validate(data)


def biz_event_to_schema(data: dict[str, Any]) -> BizEventSchema:
    return BizEventSchema.model_validate(data)


def plan_to_schema(context: TaskContext | None) -> PlanSchema | None:
    if context is None or not context.tasks:
        return None
    return PlanSchema.model_validate(context.model_dump(mode="json"))


def providers_to_schema(
    prefs: UserPreferences, registry: ProviderRegistry
) -> list[ProviderSchema]:
    return [
        ProviderSchema(
            id=spec.id,
            name=spec.name,
            chat_model=spec.chat_model,
            api_key_prefix=spec.api_key_prefix,
            catalog_base_url=spec.base_url,
            base_url=(
                spec.base_url
                if spec.id not in prefs.providers
                or prefs.providers[spec.id].base_url is None
                else prefs.providers[spec.id].base_url
            ),
            configured=spec.id in prefs.providers,
            api_key_masked=(
                mask_api_key(
                    prefs.providers[spec.id].api_key,
                    prefix=spec.api_key_prefix,
                )
                if spec.id in prefs.providers
                else ""
            ),
            models=models_to_schema(prefs, registry, spec.id),
        )
        for spec in registry.providers
        if spec.id in get_settings().catalog.provider_ids
    ]


def models_to_schema(
    prefs: UserPreferences, registry: ProviderRegistry, provider_id: str
) -> list[ProviderModelSchema]:
    spec = registry.require(provider_id)
    saved = {
        model_id: model
        for (pid, model_id), model in prefs.models.items()
        if pid == provider_id
    }
    items: list[ProviderModelSchema] = []
    for model in spec.models:
        row = saved.pop(model.id, None)
        items.append(
            ProviderModelSchema(
                id=model.id,
                name=row.name if row and row.name else model.name,
                source="override" if row else "catalog",
                context_size=model.context_size,
                thinking_enabled=None if row is None else row.thinking_enabled,
                generate_kwargs=(
                    {}
                    if row is None or not row.generate_kwargs
                    else row.generate_kwargs
                ),
                chat_model=spec.chat_model,
            )
        )
    for model_id, row in saved.items():
        items.append(
            ProviderModelSchema(
                id=model_id,
                name=row.name or model_id,
                source=row.source,  # type: ignore[arg-type]
                context_size=row.context_size,
                thinking_enabled=row.thinking_enabled,
                generate_kwargs={} if not row.generate_kwargs else row.generate_kwargs,
                chat_model=spec.chat_model,
            )
        )
    return items
