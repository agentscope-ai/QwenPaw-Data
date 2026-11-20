# -*- coding: utf-8 -*-
from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field

from qwenpaw_data.host.core.api.models.common import ApiModel


class SkillDocumentRequest(ApiModel):
    name: str = Field(min_length=1)
    content: str = Field(min_length=1)
    # SKILL.md the client started from; rejects writes over someone else's edit.
    base_content: str | None = None


class AgentSkillSchema(BaseModel):
    name: str
    enabled: bool
    source: str
    group: str = ""
    description: str = ""


class AgentSkillDetailSchema(AgentSkillSchema):
    content: str


class SkillRevisionSchema(BaseModel):
    revision_no: int
    origin: str
    source_ref: str = ""
    note: str = ""
    created_at: datetime


class SkillRevisionFileSchema(BaseModel):
    relative_path: str
    content: str


class SkillRevisionDetailSchema(SkillRevisionSchema):
    files: list[SkillRevisionFileSchema]
