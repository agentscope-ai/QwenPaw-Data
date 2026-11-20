# -*- coding: utf-8 -*-
"""Payload parsing shared by the Langfuse and HoloScope adapters."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any

from qwenpaw_data.tracing.query.backend.contract import ScoreRow


def iso(dt: datetime) -> str:
    return dt.astimezone(UTC).isoformat().replace("+00:00", "Z")


def get_time(value: object, *, required: bool = True) -> datetime | None:
    if value is None:
        if required:
            raise ValueError("start time is required")
        return None
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("datetime must be timezone-aware")
    return parsed


def read_metadata(value: object) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


def read_tags(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [tag for tag in value if isinstance(tag, str)]


def _read_float(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def read_text(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _read_numbers(value: object, kind: type[int | float]) -> dict | None:
    if not isinstance(value, dict):
        return None
    numbers = {
        str(key): kind(item)
        for key, item in value.items()
        if isinstance(item, (int, float)) and not isinstance(item, bool)
    }
    return numbers or None


def read_observation(raw: dict[str, Any]) -> dict[str, Any]:
    """Observation-level fields; Langfuse v3 and v4 use the same keys."""
    return {
        "type": read_text(raw.get("type")),
        "level": read_text(raw.get("level")),
        "status_message": read_text(raw.get("statusMessage")),
        "latency": _read_float(raw.get("latency")),
        "time_to_first_token": _read_float(raw.get("timeToFirstToken")),
        "model": read_text(raw.get("model")),
        "usage": _read_numbers(raw.get("usageDetails"), int),
        "cost": _read_numbers(raw.get("costDetails"), float),
    }


def chunks(items: list[str], size: int) -> Iterator[list[str]]:
    for start in range(0, len(items), size):
        yield items[start : start + size]


def _score_value(raw: dict[str, Any], data_type: str) -> str | float:
    value = raw.get("value")
    if data_type == "BOOLEAN":
        return "true" if value in (True, 1, "true", "1") else "false"
    if data_type == "NUMERIC":
        return float(value)
    text = raw.get("stringValue")
    if isinstance(text, str):
        return text
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return float(value)
    return str(value)


def score_row(raw: dict[str, Any], trace_id: str) -> ScoreRow:
    data_type = str(raw.get("dataType") or "")
    metadata = raw.get("metadata")
    return {
        "trace_id": trace_id,
        "id": str(raw.get("id") or ""),
        "name": str(raw.get("name") or ""),
        "value": _score_value(raw, data_type),
        "data_type": data_type,
        "comment": raw.get("comment"),
        "metadata": metadata if isinstance(metadata, dict) else None,
    }
