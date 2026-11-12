# -*- coding: utf-8 -*-
"""Build digest.md / digest.json from a session window of chats."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from qwenpaw_data.host.core.algo.skill_create.extract import (
    extract,
    render,
    serialize_evidence,
)
from qwenpaw_data.host.core.algo.skill_create.trace_export import (
    console_messages_to_trace,
    ensure_user_input,
)
from qwenpaw_data.host.core.domain.chat import Chat

DEFAULT_DIGEST_MAX_CHARS = 60000


@dataclass(frozen=True)
class Digest:
    markdown: str
    payload: dict[str, Any]
    task_instruction: str
    stats: dict[str, int]


def initial_task_instruction(chats: list[Chat]) -> str:
    """Digest §1 is the first user input of the earliest chat.

    ``Chat.user_input`` is that opening turn. Later chats, and later user
    messages inside the latest chat, are follow-ups.
    """
    if not chats:
        return ""
    first = min(chats, key=lambda item: item.sequence)
    return (first.user_input or "").strip()


def chats_to_messages(
    chats: list[Chat],
    messages_by_id: dict[str, list[dict[str, Any]]],
) -> list[dict[str, Any]]:
    """Concatenate terminal chats in sequence, restoring missing user_input."""
    out: list[dict[str, Any]] = []
    for chat in sorted(chats, key=lambda item: item.sequence):
        if not chat.is_terminal():
            continue
        converted = console_messages_to_trace(messages_by_id.get(chat.id) or [])
        converted = ensure_user_input(converted, chat.user_input)
        out.extend(converted)
    return out


def build_digest(
    messages: list[dict[str, Any]],
    *,
    task_instruction: str = "",
    max_chars: int = DEFAULT_DIGEST_MAX_CHARS,
) -> Digest:
    evidence = extract(messages, task_instruction=task_instruction)
    markdown = render(evidence, max_chars)
    payload = serialize_evidence(evidence)
    return Digest(
        markdown=markdown,
        payload=payload,
        task_instruction=str(evidence.get("task_instruction") or ""),
        stats={
            "turn_count": int(evidence["turn_count"]),
            "tool_calls": int(sum(evidence["tool_profile"].values())),
            "user_turns": len(evidence["user_turns"]),
            "errors": int(sum(evidence["errors"].values())),
        },
    )
