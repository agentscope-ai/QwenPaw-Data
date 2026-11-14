"""Inspect generic scalar and collection values without domain policy."""

from __future__ import annotations

import unicodedata
from collections.abc import Sequence


def normalize_text(value: object) -> str:
    """Normalize optional text with NFKC and collapsed whitespace, preserving case."""
    return " ".join(unicodedata.normalize("NFKC", str(value or "")).split())


def text_members(value: object) -> tuple[str, ...]:
    """Return only text members from one optional non-text sequence."""
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        return ()
    return tuple(item for item in value if isinstance(item, str))


def is_empty_value(value: object) -> bool:
    """Return whether a scalar or sequence carries no value."""
    return value is None or value == "" or value == () or value == []


__all__ = ["is_empty_value", "normalize_text", "text_members"]
