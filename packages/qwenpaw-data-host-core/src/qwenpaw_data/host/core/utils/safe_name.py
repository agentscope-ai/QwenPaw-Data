# -*- coding: utf-8 -*-
from __future__ import annotations


def require_safe_name(value: str) -> str:
    """Require a non-empty name usable as one directory or file name.

    Intended for untrusted inputs that will be written under a known root
    (for example ``agent_id`` or skill names).
    """
    if not value or not value.strip():
        raise ValueError("name is required")
    if value != value.strip():
        raise ValueError("name must not contain surrounding whitespace")
    if value in {".", ".."}:
        raise ValueError("name must not be '.' or '..'")
    if "\x00" in value or "/" in value or "\\" in value:
        raise ValueError(f"unsafe name: {value!r}")
    return value
