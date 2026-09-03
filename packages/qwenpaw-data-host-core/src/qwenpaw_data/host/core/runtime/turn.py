# -*- coding: utf-8 -*-
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from qwenpaw_data.host.core.domain.attachment import uploads_relative_path
from qwenpaw_data.host.core.domain.chat import Chat

_COMMENT_KEYS = ("path", "line_start", "line_end", "comment")
_ATTACHMENT_KEYS = ("attachment_id", "filename")


def compose_agent_input(
    text: str,
    artifact_comments: list[dict[str, Any]] | None,
    attachments: list[dict[str, Any]] | None = None,
    *,
    session_id: str | None = None,
) -> str:
    parts: list[str] = []
    refs = list(attachments or [])
    if refs:
        if not session_id:
            raise ValueError("session_id is required")
        parts.append(_compose_attachments(refs, session_id))
    comments = list(artifact_comments or [])
    if comments:
        parts.append(_compose_comments(comments))
    if text.strip():
        parts.append(text)
    if not parts:
        return text
    return "\n\n".join(parts)


def _compose_comments(comments: list[dict[str, Any]]) -> str:
    lines = ["用户对成果文件的评论："]
    for item in comments:
        missing = [key for key in _COMMENT_KEYS if item.get(key) is None]
        if missing:
            raise ValueError(f"artifact comment missing {', '.join(missing)}")
        start = item["line_start"]
        end = item["line_end"]
        loc = f"第 {start} 行" if start == end else f"第 {start}-{end} 行"
        lines.append(f"- 文件 `{item['path']}` {loc}：{item['comment']}")
    return "\n".join(lines)


def _compose_attachments(attachments: list[dict[str, Any]], session_id: str) -> str:
    lines = [
        "用户上传了以下附件（路径相对 workspace 根目录，请用 Read / Glob / Bash 等工具读取，不要凭文件名臆造内容）："
    ]
    for item in attachments:
        missing = [key for key in _ATTACHMENT_KEYS if item.get(key) is None]
        if missing:
            raise ValueError(f"attachment missing {', '.join(missing)}")
        path = uploads_relative_path(session_id, item["filename"])
        lines.append(f"- `{path}`")
    return "\n".join(lines)


@dataclass(frozen=True)
class TurnInput:
    chat_id: str
    user_input: str
    artifact_comments: list[dict[str, Any]] = field(default_factory=list)
    attachments: list[dict[str, Any]] = field(default_factory=list)
    session_id: str = ""

    @classmethod
    def from_chat(cls, chat: Chat) -> TurnInput:
        return cls(
            chat_id=chat.id,
            user_input=chat.user_input,
            artifact_comments=list(chat.artifact_comments),
            attachments=list(chat.attachments),
            session_id=chat.session_id,
        )
