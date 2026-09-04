# -*- coding: utf-8 -*-
"""Persistence contracts for chats and stream events.

Method shapes mirror the enterprise edition's repository layer so a
database-backed implementation can be substituted without touching the
runtime.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Protocol

from qwenpaw_data.host.core.api.models.cron import CronJobWrite
from qwenpaw_data.host.core.api.models.stream_objects import StreamObject
from qwenpaw_data.host.core.domain.attachment import Attachment
from qwenpaw_data.host.core.domain.chat import Chat
from qwenpaw_data.host.core.domain.preference import UserPreferences
from qwenpaw_data.host.core.domain.session import Session


class CronStore(Protocol):
    async def list(self, user_id: str) -> list[dict[str, Any]]: ...

    async def get(self, user_id: str, job_id: str) -> dict[str, Any]: ...

    async def create(
        self,
        user_id: str,
        body: CronJobWrite,
    ) -> dict[str, Any]: ...

    async def replace(
        self,
        user_id: str,
        job_id: str,
        body: CronJobWrite,
    ) -> dict[str, Any]: ...

    async def set_enabled(
        self,
        user_id: str,
        job_id: str,
        enabled: bool,
    ) -> dict[str, Any]: ...

    async def delete(self, user_id: str, job_id: str) -> None: ...

    async def get_by_id(self, job_id: str) -> dict[str, Any]:
        """Load by id without identity filter (scheduler fire path)."""
        ...

    async def list_all(self) -> list[dict[str, Any]]:
        """Load all jobs (scheduler startup)."""
        ...


class PreferencesStore(Protocol):
    async def load(self, user_id: str) -> UserPreferences: ...

    async def upsert_provider(
        self,
        user_id: str,
        provider_id: str,
        patch: dict[str, Any],
    ) -> None: ...

    async def delete_provider(self, user_id: str, provider_id: str) -> None: ...

    async def upsert_model(
        self,
        user_id: str,
        provider_id: str,
        model_id: str,
        *,
        source: str,
        name: str | None = None,
        thinking_enabled: bool | None = None,
        generate_kwargs: dict[str, Any] | None = None,
    ) -> None: ...

    async def delete_model(
        self,
        user_id: str,
        provider_id: str,
        model_id: str,
    ) -> None: ...

    async def get_active_models(self, user_id: str) -> dict[str, Any]: ...

    async def set_active_models(
        self,
        user_id: str,
        *,
        default_provider_id: str,
        default_model_id: str,
        light_provider_id: str | None = None,
        light_model_id: str | None = None,
    ) -> dict[str, Any]: ...

    async def get_runtime_settings(self, user_id: str) -> dict[str, Any]: ...

    async def set_runtime_settings(
        self,
        user_id: str,
        patch: dict[str, Any],
    ) -> dict[str, Any]: ...


class SessionStore(Protocol):
    async def add(self, session: Session) -> None: ...

    async def get(self, session_id: str) -> Session: ...

    async def save(self, session: Session) -> None: ...

    async def has_active_chat(self, session_id: str) -> bool: ...

    async def list(
        self,
        *,
        search_text: str | None = None,
        status: str | None = None,
        datasource_id: str | None = None,
        channel: str | None = None,
        sort: str = "updated_desc",
        page: int = 1,
        page_size: int = 20,
    ) -> tuple[list[tuple[Session, bool]], int]: ...

    async def delete(self, session_id: str) -> None: ...


class ChatStore(Protocol):
    async def add(self, chat: Chat) -> None: ...

    async def get(
        self,
        chat_id: str,
        *,
        session_id: str | None = None,
    ) -> Chat: ...

    async def save(self, chat: Chat) -> None: ...

    async def get_active_for_session(self, session_id: str) -> Chat | None: ...

    async def list_for_session(self, session_id: str) -> list[Chat]: ...

    async def list_active(self) -> list[Chat]: ...

    async def update_plan(self, chat_id: str, plan: dict[str, Any]) -> None: ...

    async def reload_event_watermark(self, chat: Chat) -> None: ...


class AttachmentStore(Protocol):
    """Uploaded-file metadata; payload bytes live under the shared workspace."""

    async def add(self, attachment: Attachment) -> None: ...

    async def get(self, user_id: str, attachment_id: str) -> Attachment: ...

    async def find_by_filename(
        self,
        user_id: str,
        session_id: str,
        filename: str,
    ) -> Attachment | None: ...

    async def require_for_session(
        self,
        user_id: str,
        session_id: str,
        attachment_ids: list[str],
        *,
        workspace: Path,
    ) -> list[Attachment]: ...

    async def delete(self, user_id: str, attachment_id: str) -> None: ...


class FeedbackStore(Protocol):
    """Chat feedback: one mutable like/dislike reaction plus append-only rest."""

    async def add(
        self,
        *,
        user_id: str,
        session_id: str,
        chat_id: str,
        kind: str,
        reason: str | None = None,
        detail: str | None = None,
        artifact_ref: dict[str, Any] | None = None,
    ) -> dict[str, Any]: ...

    async def set_reaction(
        self,
        *,
        user_id: str,
        session_id: str,
        chat_id: str,
        kind: str,
        reason: str | None = None,
        detail: str | None = None,
    ) -> dict[str, Any]: ...

    async def get_reaction(
        self,
        user_id: str,
        session_id: str,
        chat_id: str,
    ) -> dict[str, Any] | None: ...

    async def clear_reaction(
        self,
        user_id: str,
        session_id: str,
        chat_id: str,
    ) -> None: ...


class ChatEventStore(Protocol):
    async def append(
        self,
        *,
        session_id: str,
        chat_id: str,
        payload: dict[str, Any],
    ) -> StreamObject: ...

    async def read_after(self, chat_id: str, after: int) -> list[StreamObject]: ...

    async def last_sequence_number(self, chat_id: str) -> int: ...


class SettlementStore(Protocol):
    """Settlement cards; every list is ordered created_at descending."""

    async def add(
        self,
        *,
        user_id: str,
        session_id: str,
        source_chat_id: str,
        type: str,
        fields: dict[str, str],
    ) -> dict[str, Any]: ...

    async def list_by_session(
        self,
        user_id: str,
        session_id: str,
        status: str | None = None,
    ) -> list[dict[str, Any]]: ...

    async def get(
        self,
        user_id: str,
        card_id: str,
        *,
        session_id: str,
    ) -> dict[str, Any]: ...

    async def mark_queried(
        self,
        user_id: str,
        session_id: str,
        card_ids: list[str],
    ) -> None: ...

    async def confirm(
        self,
        user_id: str,
        card_id: str,
        *,
        session_id: str,
        fields: dict[str, str] | None = None,
    ) -> dict[str, Any]: ...

    async def dismiss(
        self,
        user_id: str,
        card_id: str,
        *,
        session_id: str,
    ) -> dict[str, Any]: ...

    async def delete_if_unconfirmed(
        self,
        user_id: str,
        card_id: str,
        *,
        session_id: str,
    ) -> bool: ...


class ChannelConfigStore(Protocol):
    """Per-user IM channel configuration (secrets included; mask at the edge)."""

    async def load(self, user_id: str) -> dict[str, Any]:
        """Full config for a user; defaults when the user has no row yet."""
        ...

    async def save(self, user_id: str, config: dict[str, Any]) -> None: ...

    async def list_user_ids(self) -> list[str]:
        """Users that have a stored channel configuration."""
        ...


class ChannelBindingStore(Protocol):
    """``(user_id, channel, external_key)`` → active session pointer + send address."""

    async def get_active_session_id(
        self,
        user_id: str,
        channel: str,
        external_key: str,
    ) -> str | None: ...

    async def point_to(
        self,
        user_id: str,
        channel: str,
        external_key: str,
        session_id: str,
        *,
        target_meta: dict[str, Any] | None = None,
        display_name: str = "",
    ) -> None: ...

    async def list_by_channel(
        self,
        user_id: str,
        channel: str,
    ) -> list[dict[str, Any]]: ...

    async def get_target_meta(
        self,
        user_id: str,
        channel: str,
        external_key: str,
    ) -> dict[str, Any] | None: ...

    async def exists(
        self,
        user_id: str,
        channel: str,
        external_key: str,
    ) -> bool: ...
