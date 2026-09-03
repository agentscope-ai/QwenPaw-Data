# -*- coding: utf-8 -*-
from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import Field

from qwenpaw_data.host.core.api.models.common import ApiModel


class TaskSchema(ApiModel):
    id: str
    subject: str
    description: str
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: str = Field(default_factory=lambda: datetime.now().isoformat())
    state: Literal["pending", "in_progress", "completed"] = "pending"
    owner: str | None = None
    blocks: list[str] = Field(default_factory=list)
    blocked_by: list[str] = Field(default_factory=list)


class PlanSchema(ApiModel):
    tasks: list[TaskSchema] = Field(default_factory=list)
