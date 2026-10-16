# -*- coding: utf-8 -*-
from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import Field

from qwenpaw_data.host.core.api.models.common import ApiModel


class ArtifactPresentationSchema(ApiModel):
    """An explicit deliverable declaration, independent of its filename."""

    schema_version: Literal[1] = 1
    role: Literal["primary", "supporting", "diagnostic", "source"]
    kind: str = Field(
        pattern=r"^[a-z0-9][a-z0-9._-]*/[a-z0-9][a-z0-9._-]*$", max_length=128
    )
    visibility: Literal["chat", "app_only"] = "app_only"
    preview: Literal["inline", "link", "none"] = "link"
    rank: int = Field(default=100, ge=0, le=10000)


class ArtifactSchema(ApiModel):
    id: str
    session_id: str
    chat_id: str | None = None
    name: str
    path: str
    # Added in the artifact-handoff capability. Optional only so historical
    # stream rows created by older Engines remain readable.
    media_type: str | None = None
    size_bytes: int | None = None
    digest: str | None = None
    presentation: ArtifactPresentationSchema | None = None
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
