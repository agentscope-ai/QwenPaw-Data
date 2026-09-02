# -*- coding: utf-8 -*-
"""Data types for the channel subsystem.

Two categories:
1. ``ChannelType`` — built-in channel identifiers (console/feishu/dingtalk/wecom/wechat).
2. ``Content`` types (``TextContent``/``ImageContent``/...) — used by the channel side to parse
   inbound messages; a self-defined copy that does not depend on qwenpaw.schemas.

native payload convention (produced by channel inbound handlers, consumed by ``_consume_one_request``)::

    {
        "channel_id": "feishu",
        "sender_id": "<platform sender id>",
        "content_parts": [TextContent(...), ImageContent(...), ...],
        "meta": {<platform-specific fields: chat_id, session_webhook, ...>},
    }
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class ChannelType(str, Enum):
    CONSOLE = "console"
    FEISHU = "feishu"
    DINGTALK = "dingtalk"
    WECOM = "wecom"
    WECHAT = "wechat"


# ---------------------------------------------------------------------------
# Content types (isomorphic to QwenPaw schemas.Content).
# Channels produce content_parts: list[Content] when parsing inbound messages;
# _consume_one_request flattens content_parts into str at the channel->runtime boundary
# (concatenating TextContent.text; non-text becomes [image]/[audio]/[file] placeholders).
# The current runtime agent only consumes str; once multimodal support lands, drop the
# flattening — channel parsing code stays unchanged.
# ---------------------------------------------------------------------------


@dataclass
class Content:
    """Content base class."""

    type: str


@dataclass
class TextContent(Content):
    type: str = "text"
    text: str = ""


@dataclass
class ImageContent(Content):
    type: str = "image"
    url: str | None = None
    file_key: str | None = None


@dataclass
class AudioContent(Content):
    type: str = "audio"
    url: str | None = None
    file_key: str | None = None
    duration_ms: int | None = None


@dataclass
class VideoContent(Content):
    type: str = "video"
    url: str | None = None
    file_key: str | None = None


@dataclass
class FileContent(Content):
    type: str = "file"
    url: str | None = None
    file_key: str | None = None
    filename: str | None = None


@dataclass
class NativePayload:
    """Native payload produced by channel inbound handlers (see module docstring)."""

    channel_id: str
    sender_id: str
    content_parts: list[Content] = field(default_factory=list)
    meta: dict[str, Any] = field(default_factory=dict)


# Placeholders for non-text content (used when flattening at the boundary).
_PLACEHOLDER = {
    "image": "[图片]",
    "audio": "[语音]",
    "video": "[视频]",
    "file": "[文件]",
}


def flatten_content_parts_to_str(content_parts: list[Content]) -> str:
    """Flatten content_parts into str (at the channel->runtime boundary).

    TextContent contributes its text; non-text contributes a placeholder from
    ``_PLACEHOLDER``. The current runtime agent only consumes str
    (``UserMsg(content=turn.user_input)``).
    """
    parts: list[str] = []
    for c in content_parts:
        if c.type == "text":
            parts.append(getattr(c, "text", "") or "")
        else:
            parts.append(_PLACEHOLDER.get(c.type, f"[{c.type}]"))
    return "".join(parts).strip()
