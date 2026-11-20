"""Parse and validate business-neutral JSON transport shapes."""

from __future__ import annotations

import json
from collections.abc import Collection


def parse_json_object(value: object, field_name: str) -> dict[str, object]:
    """Parse one plain or fenced JSON object without accepting prose."""
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be non-empty text")
    payload = value.strip()
    if payload.startswith("```json") and payload.endswith("```"):
        payload = payload[7:-3].strip()
    elif payload.startswith("```") and payload.endswith("```"):
        payload = payload[3:-3].strip()
    parsed = json.loads(payload)
    return require_json_object(parsed, field_name)


def require_json_array(value: object, field_name: str) -> list[object]:
    """Return one JSON array without accepting text or arbitrary iterables."""
    if type(value) is not list:
        raise TypeError(f"{field_name} must be an array")
    return value


def require_json_object(value: object, field_name: str) -> dict[str, object]:
    """Return one concrete JSON object whose property names are text."""
    if type(value) is not dict:
        raise TypeError(f"{field_name} must be an object")
    if any(not isinstance(name, str) for name in value):
        raise TypeError(f"{field_name} keys must be text")
    return value


def require_json_object_fields(
    value: object,
    field_name: str,
    *,
    allowed: Collection[str],
    required: Collection[str],
) -> dict[str, object]:
    """Return one text-keyed object with an exact bounded field envelope."""
    document = require_json_object(value, f"{field_name} entry")
    allowed_fields = frozenset(allowed)
    required_fields = frozenset(required)
    unknown = set(document).difference(allowed_fields)
    missing = required_fields.difference(document)
    if unknown or missing:
        raise ValueError(
            f"{field_name} entry fields are invalid: "
            f"unknown={sorted(unknown)}, missing={sorted(missing)}"
        )
    return document


__all__ = [
    "parse_json_object",
    "require_json_array",
    "require_json_object",
    "require_json_object_fields",
]
