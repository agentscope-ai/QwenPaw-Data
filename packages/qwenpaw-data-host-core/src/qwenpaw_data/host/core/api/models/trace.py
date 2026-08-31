# -*- coding: utf-8 -*-
from __future__ import annotations

from typing import Annotated, Any, Literal, Union

from pydantic import ConfigDict, Field

from qwenpaw_data.host.core.api.models.common import ApiModel


class ContentBaseSchema(ApiModel):
    """QwenPaw-aligned content base: modality-specific subclasses only."""

    model_config = ConfigDict(extra="ignore")

    object: Literal["content"] = "content"
    delta: bool = False
    index: int | None = None
    status: str | None = None
    msg_id: str | None = None


class TextContentSchema(ContentBaseSchema):
    type: Literal["text"] = "text"
    text: str = ""


class ImageContentSchema(ContentBaseSchema):
    type: Literal["image"] = "image"
    image_url: str | None = None


class AudioContentSchema(ContentBaseSchema):
    type: Literal["audio"] = "audio"
    data: str | None = None
    format: str | None = None


class VideoContentSchema(ContentBaseSchema):
    type: Literal["video"] = "video"
    video_url: str | None = None


class FileContentSchema(ContentBaseSchema):
    type: Literal["file"] = "file"
    filename: str | None = None
    file_url: str | None = None


class DataContentSchema(ContentBaseSchema):
    type: Literal["data"] = "data"
    data: Any = None


class RefusalContentSchema(ContentBaseSchema):
    type: Literal["refusal"] = "refusal"
    refusal: str = ""


ContentSchema = Annotated[
    Union[
        TextContentSchema,
        ImageContentSchema,
        AudioContentSchema,
        VideoContentSchema,
        FileContentSchema,
        DataContentSchema,
        RefusalContentSchema,
    ],
    Field(discriminator="type"),
]


class MessageSchema(ApiModel):
    id: str
    object: Literal["message"] = "message"
    chat_id: str | None = None
    sequence: int
    type: str
    role: Literal["user", "assistant", "system", "tool"] | None = None
    content: list[ContentSchema] = []
    status: Literal["created", "in_progress", "completed", "failed", "cancelled"] = (
        "completed"
    )
    source_id: str | None = None
    metadata: dict[str, Any] | None = None


class PresentationSchema(ApiModel):
    card_type: Literal["user", "thinking", "text", "hint", "tool"]
    caption: str
    body: str = ""


class BizEventSchema(ApiModel):
    event_id: str
    chat_id: str | None = None
    seq: int
    channel: Literal["main", "subagent"] = "main"
    block_id: str | None = None
    status: Literal["done", "error"] = "done"
    presentation: PresentationSchema | None = None
    started_at: float = 0.0
    ended_at: float | None = None


class SegmentArtifactSchema(ApiModel):
    name: str
    description: str = ""
    relative_path: str | None = None


class CoverageSchema(ApiModel):
    start_seq: int
    end_seq: int


class SegmentSchema(ApiModel):
    segment_id: str
    chat_id: str | None = None
    title: str
    input: str | None = None
    behavior: str = ""
    conclusion: str = ""
    artifact: list[SegmentArtifactSchema] | None = None
    coverage: CoverageSchema
    started_at: float | None = None
    ended_at: float | None = None
