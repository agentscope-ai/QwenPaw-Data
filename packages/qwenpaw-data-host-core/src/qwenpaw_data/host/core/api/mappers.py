# -*- coding: utf-8 -*-
from __future__ import annotations

from typing import Any

from qwenpaw_data.host.core.api.models.chat import (
    ChatErrorSchema,
    ChatSchema,
    SessionSchema,
)
from qwenpaw_data.host.core.api.models.preferences import (
    ProviderModelSchema,
    ProviderSchema,
)
from qwenpaw_data.host.core.domain.chat import Chat
from qwenpaw_data.host.core.domain.preference import UserPreferences
from qwenpaw_data.host.core.domain.session import Session
from qwenpaw_data.host.core.providers.registry import ProviderRegistry
from qwenpaw_data.host.core.utils.plan import sop_plan_to_schema
from qwenpaw_data.host.core.utils.secrets import mask_api_key


def providers_to_schema(
    prefs: UserPreferences,
    registry: ProviderRegistry,
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
    ]


def models_to_schema(
    prefs: UserPreferences,
    registry: ProviderRegistry,
    provider_id: str,
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
                thinking_enabled=row.thinking_enabled,
                generate_kwargs={} if not row.generate_kwargs else row.generate_kwargs,
                chat_model=spec.chat_model,
            )
        )
    return items


def session_to_schema(
    session: Session,
    *,
    has_active_chat: bool,
) -> dict[str, Any]:
    return SessionSchema(
        id=session.id,
        agent_id=session.agent_id,
        title=session.title,
        status=session.derive_status(  # type: ignore[arg-type]
            has_active_chat=has_active_chat,
        ),
        datasource_id=session.datasource_id,
        chat_count=session.chat_count,
        channel=session.channel,
        parent_session_id=session.parent_session_id,
        forked_from_chat_id=session.forked_from_chat_id,
        created_at=session.created_at,
        updated_at=session.updated_at,
    ).model_dump(mode="json")


def chat_to_schema(
    chat: Chat,
    extras: dict[str, Any] | None = None,
) -> dict[str, Any]:
    bundle = extras or {}
    return ChatSchema(
        id=chat.id,
        session_id=chat.session_id,
        sequence=chat.sequence,
        user_input=chat.user_input,
        datasource_id=chat.datasource_id,
        kind=chat.kind,  # type: ignore[arg-type]
        status=chat.status,  # type: ignore[arg-type]
        last_sequence_number=chat.last_sequence_number,
        started_at=chat.started_at,
        completed_at=chat.completed_at,
        active_duration_ms=chat.active_duration_ms,
        error=ChatErrorSchema(**chat.error) if chat.error else None,
        plan=sop_plan_to_schema(chat.plan),
        segments=bundle.get("segments") or [],
        biz_events=bundle.get("biz_events") or [],
        artifacts=bundle.get("artifacts") or [],
        followup=bundle.get("followup"),
        artifact_comments=chat.artifact_comments,  # type: ignore[arg-type]
        attachments=chat.attachments,  # type: ignore[arg-type]
    ).model_dump(mode="json")
