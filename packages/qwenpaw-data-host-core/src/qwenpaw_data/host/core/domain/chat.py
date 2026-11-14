# -*- coding: utf-8 -*-
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from agentscope.state import TaskContext

from qwenpaw_data.host.core.domain.chat_failure import ChatFailure
from qwenpaw_data.host.core.domain.identity import Identity
from qwenpaw_data.host.core.settings import get_settings
from qwenpaw_data.host.core.utils.ids import create_id
from qwenpaw_data.host.core.utils.time import utcnow

ACTIVE = ("running",)
TERMINAL = ("completed", "failed", "canceled")


@dataclass
class Chat:
    id: str
    session_id: str
    identity: Identity
    sequence: int
    user_input: str
    datasource_id: str | None
    kind: str
    status: str
    last_sequence_number: int
    started_at: datetime | None
    completed_at: datetime | None
    active_duration_ms: int
    error: dict[str, Any] | None
    plan: TaskContext
    created_at: datetime
    updated_at: datetime
    artifact_comments: list[dict[str, Any]] = field(default_factory=list)
    attachments: list[dict[str, Any]] = field(default_factory=list)
    agent_state: dict[str, Any] | None = None
    dataset_names: list[str] | None = None
    enable_clarification: bool = True
    heartbeat_at: datetime | None = None
    otel_span_id: str | None = None

    @classmethod
    def start(
        cls,
        *,
        session_id: str,
        identity: Identity,
        sequence: int,
        datasource_id: str | None,
        text: str,
        artifact_comments: list[dict[str, Any]] | None = None,
        attachments: list[dict[str, Any]] | None = None,
        dataset_names: list[str] | None = None,
        enable_clarification: bool = True,
    ) -> Chat:
        refs = list(attachments or [])
        if not text.strip() and not refs:
            raise ValueError("text is required")

        now = utcnow()
        return cls(
            id=create_id("chat"),
            session_id=session_id,
            identity=identity,
            sequence=sequence,
            user_input=text,
            datasource_id=datasource_id,
            kind="simple",
            status="running",
            last_sequence_number=-1,
            started_at=now,
            completed_at=None,
            active_duration_ms=0,
            error=None,
            plan=TaskContext(),
            created_at=now,
            updated_at=now,
            artifact_comments=list(artifact_comments or []),
            attachments=refs,
            dataset_names=dataset_names or None,
            enable_clarification=enable_clarification,
            heartbeat_at=now,
        )

    def set_enable_clarification(self, enable_clarification: bool) -> None:
        if self.enable_clarification == enable_clarification:
            return
        self.enable_clarification = enable_clarification
        self.updated_at = utcnow()

    def is_active(self) -> bool:
        return self.status in ACTIVE

    def is_terminal(self) -> bool:
        return self.status in TERMINAL

    def is_heartbeat_fresh(self) -> bool:
        at = self.heartbeat_at or self.started_at
        ttl = get_settings().stream.heartbeat_ttl_seconds
        return at is not None and utcnow() - at <= timedelta(seconds=ttl)

    def cancel(self) -> None:
        if self.is_terminal():
            return
        if not self.is_active():
            raise RuntimeError("CONFLICT: chat is not active")
        now = utcnow()
        self.status = "canceled"
        self.completed_at = now
        self.updated_at = now

    def mark_failed(self, failure: ChatFailure) -> None:
        if not self.is_active():
            raise RuntimeError("CONFLICT: chat is not active")
        self.error = failure.to_dict()
        self.mark_status("failed")

    def mark_status(self, status: str) -> None:
        self.status = status
        self.updated_at = utcnow()
        if self.is_terminal():
            self.completed_at = self.updated_at

    def apply_event_watermark(
        self, *, last_sequence_number: int, updated_at: datetime
    ) -> None:
        self.last_sequence_number = last_sequence_number
        self.updated_at = updated_at

    def require_active_for_steer(self) -> None:
        if not self.is_active():
            raise RuntimeError("CONFLICT: no active chat")

    def references_attachment(self, attachment_id: str) -> bool:
        return any(
            item.get("attachment_id") == attachment_id for item in self.attachments
        )
