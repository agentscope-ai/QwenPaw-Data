# -*- coding: utf-8 -*-
from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any, Literal, Union

from pydantic import Field, TypeAdapter, model_validator

from qwenpaw_data.host.core.api.models.artifact import ArtifactSchema
from qwenpaw_data.host.core.api.models.chat import ChatErrorSchema, FollowUpSchema
from qwenpaw_data.host.core.api.models.common import ApiModel, ErrorBodySchema
from qwenpaw_data.host.core.api.models.trace import (
    BizEventSchema,
    ContentSchema,
    MessageSchema,
    SegmentSchema,
)


class StreamBaseSchema(ApiModel):
    sequence_number: int
    session_id: str
    chat_id: str
    created_at: datetime


class ResponseEventSchema(StreamBaseSchema):
    object: Literal["response"] = "response"
    id: str
    status: Literal["created", "in_progress", "completed", "failed", "cancelled"]
    usage: dict[str, Any] | None = None
    error: ChatErrorSchema | None = None


class MessageEventSchema(StreamBaseSchema, MessageSchema):
    object: Literal["message"] = "message"


class ContentEventSchema(StreamBaseSchema):
    """SSE content event; modality fields match QwenPaw *Content variants."""

    object: Literal["content"] = "content"
    msg_id: str
    type: Literal["text", "image", "audio", "video", "data", "file", "refusal"]
    delta: bool = False
    index: int | None = None
    status: str | None = None
    text: str | None = None
    image_url: str | None = None
    data: Any | None = None
    format: str | None = None
    video_url: str | None = None
    filename: str | None = None
    file_url: str | None = None
    refusal: str | None = None

    @model_validator(mode="after")
    def _validate_modality(self) -> ContentEventSchema:
        # Coerce through the typed content variant so unknown combos fail fast.
        body = {
            "object": "content",
            "type": self.type,
            "delta": self.delta,
            "index": self.index,
            "status": self.status,
            "msg_id": self.msg_id,
            "text": self.text,
            "image_url": self.image_url,
            "data": self.data,
            "format": self.format,
            "video_url": self.video_url,
            "filename": self.filename,
            "file_url": self.file_url,
            "refusal": self.refusal,
        }
        _CONTENT_ADAPTER.validate_python(body)
        return self


class ErrorEventSchema(StreamBaseSchema):
    object: Literal["error"] = "error"
    code: Literal["UNAUTHORIZED", "FORBIDDEN", "NOT_FOUND", "CONFLICT", "VALIDATION"]
    message: str
    details: dict[str, Any] | None = None


class SegmentEventSchema(StreamBaseSchema):
    object: Literal["segment"] = "segment"
    segment: SegmentSchema


class BizEventEventSchema(StreamBaseSchema):
    object: Literal["biz_event"] = "biz_event"
    biz_event: BizEventSchema


class ArtifactRegisteredEventSchema(StreamBaseSchema):
    object: Literal["artifact.registered"] = "artifact.registered"
    artifact: ArtifactSchema


class FollowUpGeneratedEventSchema(StreamBaseSchema):
    object: Literal["followup.generated"] = "followup.generated"
    followup: FollowUpSchema


class ContextUsageEventSchema(StreamBaseSchema):
    object: Literal["context.usage"] = "context.usage"
    context_size: int
    used_tokens: int
    trigger_ratio: float
    reserve_ratio: float
    tool_result_limit: int


class McpConnectEventSchema(StreamBaseSchema):
    object: Literal["mcp.connect"] = "mcp.connect"
    mcp_name: str
    status: Literal["connected", "failed"]
    error: str | None = None
    url: str | None = None
    command: str | None = None


class ModelCallEventSchema(StreamBaseSchema):
    object: Literal["model.call"] = "model.call"
    model_name: str


class ModelCallEndEventSchema(StreamBaseSchema):
    object: Literal["model.call.end"] = "model.call.end"
    input_tokens: int = 0
    output_tokens: int = 0


class ToolResultEndEventSchema(StreamBaseSchema):
    object: Literal["tool.result.end"] = "tool.result.end"
    tool_name: str
    call_id: str


class ToolResultStartEventSchema(StreamBaseSchema):
    object: Literal["tool.result.start"] = "tool.result.start"
    tool_name: str
    call_id: str


StreamObject = Annotated[
    Union[
        ResponseEventSchema,
        MessageEventSchema,
        ContentEventSchema,
        ErrorEventSchema,
        SegmentEventSchema,
        BizEventEventSchema,
        ArtifactRegisteredEventSchema,
        FollowUpGeneratedEventSchema,
        ContextUsageEventSchema,
        McpConnectEventSchema,
        ModelCallEventSchema,
        ModelCallEndEventSchema,
        ToolResultEndEventSchema,
        ToolResultStartEventSchema,
    ],
    Field(discriminator="object"),
]

_ADAPTER: TypeAdapter[StreamObject] = TypeAdapter(StreamObject)
_CONTENT_ADAPTER: TypeAdapter[ContentSchema] = TypeAdapter(ContentSchema)


def parse_stream_object(data: dict[str, Any]) -> StreamObject:
    return _ADAPTER.validate_python(data)


def dump_stream_object(obj: StreamObject) -> dict[str, Any]:
    """Serialize like QwenPaw wire: omit null modality / optional fields."""
    return obj.model_dump(mode="json", exclude_none=True)
