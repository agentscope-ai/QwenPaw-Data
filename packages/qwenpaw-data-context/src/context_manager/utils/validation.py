"""Validate business-neutral scalar shapes at module boundaries."""

from __future__ import annotations

import math


def require_nonempty_text(value: object, field_name: str) -> str:
    """Return text containing at least one non-whitespace character."""
    if not isinstance(value, str):
        raise TypeError(f"{field_name} must be text")
    if not value.strip():
        raise ValueError(f"{field_name} must be non-empty text")
    return value


def normalize_nonempty_text(value: object, field_name: str) -> str:
    """Return non-empty text after removing surrounding whitespace."""
    if not isinstance(value, str):
        raise TypeError(f"{field_name} must be text")
    normalized = value.strip()
    if not normalized:
        raise ValueError(f"{field_name} must be non-empty text")
    return normalized


def require_trimmed_text(value: object, field_name: str) -> str:
    """Return non-empty text that is already trimmed."""
    if not isinstance(value, str):
        raise TypeError(f"{field_name} must be text")
    if not value or value != value.strip():
        raise ValueError(f"{field_name} must be non-empty and trimmed")
    return value


def require_path_component(value: object, field_name: str) -> str:
    """Return one trimmed path component without traversal or separators."""
    component = require_trimmed_text(value, field_name)
    if component in {".", ".."} or any(
        char in component for char in ("/", "\\", "\x00")
    ):
        raise ValueError(f"{field_name} must be a single path component")
    return component


def optional_trimmed_text(value: object | None, field_name: str) -> str | None:
    """Return absent text unchanged and validate a supplied value."""
    if value is None:
        return None
    return require_trimmed_text(value, field_name)


def require_integer(
    value: object,
    field_name: str,
    *,
    minimum: int,
    maximum: int,
) -> int:
    """Return one non-boolean integer inside an inclusive finite range."""
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{field_name} must be an integer")
    if not minimum <= value <= maximum:
        raise ValueError(f"{field_name} must be between {minimum} and {maximum}")
    return value


def require_probability(value: object, field_name: str) -> float:
    """Return one finite numeric value in the inclusive unit interval."""
    if (
        not isinstance(value, (int, float))
        or isinstance(value, bool)
        or not math.isfinite(float(value))
        or not 0 <= float(value) <= 1
    ):
        raise ValueError(f"{field_name} must be between zero and one")
    return float(value)


__all__ = [
    "normalize_nonempty_text",
    "optional_trimmed_text",
    "require_integer",
    "require_nonempty_text",
    "require_path_component",
    "require_probability",
    "require_trimmed_text",
]
