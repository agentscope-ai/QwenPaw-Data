# -*- coding: utf-8 -*-
from __future__ import annotations

_RESPONSE_ACTIVE = frozenset({"created", "in_progress"})
_RESPONSE_TERMINAL = frozenset({"completed", "failed", "cancelled", "canceled"})


def is_active_response(obj: object) -> bool:
    return (
        getattr(obj, "object", None) == "response"
        and getattr(obj, "status", None) in _RESPONSE_ACTIVE
    )


def is_terminal_response(obj: object) -> bool:
    return (
        getattr(obj, "object", None) == "response"
        and getattr(obj, "status", None) in _RESPONSE_TERMINAL
    )


def chat_status_from_response(status: str | None) -> str | None:
    if status in _RESPONSE_ACTIVE:
        return None
    if status not in _RESPONSE_TERMINAL:
        raise ValueError(f"unknown response status: {status!r}")
    return "canceled" if status == "cancelled" else status
