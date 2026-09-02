# -*- coding: utf-8 -*-
from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from qwenpaw_data.host.core.api.models.common import ApiModel


class ChatErrorSchema(ApiModel):
    code: str
    message: str


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
