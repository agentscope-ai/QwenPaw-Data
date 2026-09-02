# -*- coding: utf-8 -*-
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from qwenpaw_data.host.core.domain.identity import Identity
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
    plan: dict[str, Any] | None
    created_at: datetime
    updated_at: datetime

    @classmethod
    def start(
        cls,
        *,
        session_id: str,
        identity: Identity,
        sequence: int,
        datasource_id: str | None,
        text: str,
    ) -> Chat:
        if not text.strip():
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
            plan=None,
            created_at=now,
            updated_at=now,
        )

    def cancel(self) -> None:
        if self.status in TERMINAL:
            return
        if self.status not in ACTIVE:
            raise RuntimeError("CONFLICT: chat is not active")
        now = utcnow()
        self.status = "canceled"
        self.completed_at = now
        self.updated_at = now

    def mark_status(self, status: str) -> None:
        self.status = status
        self.updated_at = utcnow()
        if status in TERMINAL:
            self.completed_at = self.updated_at

    def apply_event_watermark(
        self, *, last_sequence_number: int, updated_at: datetime
    ) -> None:
        self.last_sequence_number = last_sequence_number
        self.updated_at = updated_at

    def require_active_for_steer(self) -> None:
        if self.status not in ACTIVE:
            raise RuntimeError("CONFLICT: no active chat")
