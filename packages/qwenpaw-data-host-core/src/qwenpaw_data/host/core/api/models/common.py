# -*- coding: utf-8 -*-
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict


class ApiModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ErrorBodySchema(ApiModel):
    code: Literal["UNAUTHORIZED", "FORBIDDEN", "NOT_FOUND", "CONFLICT", "VALIDATION"]
    message: str
    details: dict[str, Any] | None = None


class AttachmentRefSchema(ApiModel):
    attachment_id: str
    filename: str


class DatasourceOptionSchema(ApiModel):
    id: str
    name: str
    status: str
    description: str = ""
    recommended: bool = False
