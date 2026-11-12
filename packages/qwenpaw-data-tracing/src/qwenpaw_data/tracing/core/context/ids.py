# -*- coding: utf-8 -*-
"""Optional bound trace id for a new root (does not reuse a chat id)."""

from __future__ import annotations

import uuid
from contextvars import ContextVar, Token

from opentelemetry.sdk.trace.id_generator import RandomIdGenerator

_bound_trace_id: ContextVar[str | None] = ContextVar(
    "qwenpaw_data_bound_trace_id",
    default=None,
)


def new_trace_id() -> str:
    return uuid.uuid4().hex


def bind_trace_id(trace_id: str) -> Token[str | None]:
    return _bound_trace_id.set(trace_id)


def reset_bound_trace_id(token: Token[str | None]) -> None:
    _bound_trace_id.reset(token)


class BoundIdGenerator(RandomIdGenerator):
    """Root spans take the bound id when one is set."""

    def generate_trace_id(self) -> int:
        raw = _bound_trace_id.get()
        if raw:
            return int(raw, 16)
        return super().generate_trace_id()


__all__ = [
    "BoundIdGenerator",
    "bind_trace_id",
    "new_trace_id",
    "reset_bound_trace_id",
]
