# -*- coding: utf-8 -*-
"""Chat-id ↔ trace-id binding. Host-only."""

from __future__ import annotations

import re
from contextvars import ContextVar, Token

from qwenpaw_data.tracing.core.context.ids import BoundIdGenerator, _bound_trace_id
from opentelemetry.sdk.trace.id_generator import RandomIdGenerator

_CHAT_TRACE_ID = re.compile(r"^chat-([0-9a-f]{32})$")
_chat_trace_id: ContextVar[str | None] = ContextVar(
    "qwenpaw_data_chat_trace_id",
    default=None,
)


def trace_id_from_chat_id(chat_id: str) -> str | None:
    """Return the 32-hex trace id for a hyphen-free chat id."""
    match = _CHAT_TRACE_ID.fullmatch(chat_id.strip())
    if match is None:
        return None
    return match.group(1)


def bind_chat_trace(chat_id: str) -> Token[str | None]:
    """Use this chat's trace id for root spans started until reset."""
    return set_chat_trace(trace_id_from_chat_id(chat_id))


def set_chat_trace(trace_id: str | None) -> Token[str | None]:
    return _chat_trace_id.set(trace_id)


def reset_chat_trace(token: Token[str | None]) -> None:
    _chat_trace_id.reset(token)


class ChatTraceIdGenerator(BoundIdGenerator):
    """Root spans take a bound new-trace id, else the chat id, else random."""

    def generate_trace_id(self) -> int:
        bound = _bound_trace_id.get()
        if bound:
            return int(bound, 16)
        raw = _chat_trace_id.get()
        if raw:
            return int(raw, 16)
        return RandomIdGenerator.generate_trace_id(self)
