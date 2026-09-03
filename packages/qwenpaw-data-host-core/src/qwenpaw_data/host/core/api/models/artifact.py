# -*- coding: utf-8 -*-
from __future__ import annotations

from datetime import datetime

from qwenpaw_data.host.core.api.models.common import ApiModel


class ArtifactSchema(ApiModel):
    id: str
    session_id: str
    chat_id: str | None = None
    name: str
    path: str
    created_at: datetime
    updated_at: datetime


class ArtifactLineRefSchema(ApiModel):
    artifact_id: str
    content_hash: str
    line_start: int
    line_end: int
    quote: str


class ArtifactCommentSchema(ApiModel):
    path: str
    line_start: int
    line_end: int
    comment: str


class ShareFileRequest(ApiModel):
    path: str


class ShareFileResponse(ApiModel):
    url: str
    expires_at: datetime
    name: str
