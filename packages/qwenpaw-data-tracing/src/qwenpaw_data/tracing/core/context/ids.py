# -*- coding: utf-8 -*-
"""Trace id format, and an optional bound trace id for a new root."""

from __future__ import annotations

import re
import uuid
from contextvars import ContextVar, Token

from opentelemetry.sdk.trace.id_generator import RandomIdGenerator

TRACE_ID = r"[0-9a-f]{32}"
_TRACE_ID = re.compile(TRACE_ID)

_bound_trace_id: ContextVar[str | None] = ContextVar(
    "qwenpaw_data_bound_trace_id",
    default=None,
)


def new_trace_id() -> str:
    return uuid.uuid4().hex


def check_trace_id(value: object, name: str = "trace_id") -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} is required")
    trace_id = value.strip()
    if _TRACE_ID.fullmatch(trace_id) is None:
        raise ValueError(f"{name} must be 32 lowercase hex characters")
    return trace_id


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
    "TRACE_ID",
    "BoundIdGenerator",
    "bind_trace_id",
    "check_trace_id",
    "new_trace_id",
    "reset_bound_trace_id",
]
