# -*- coding: utf-8 -*-
"""JSON-safe serialization for span attributes. No OpenTelemetry imports."""

from __future__ import annotations

import json
from typing import Any


def plain(value: Any) -> Any:
    """Convert pydantic models and containers into JSON-safe structures."""
    if isinstance(value, list):
        return [plain(item) for item in value]
    if isinstance(value, dict):
        return {str(key): plain(item) for key, item in value.items()}
    dump = getattr(value, "model_dump", None)
    if callable(dump):
        try:
            return dump(mode="json")
        except TypeError:
            try:
                return dump()
            except Exception:
                return str(value)
        except Exception:
            return str(value)
    return value


def dump_json(value: Any) -> str:
    """Serialize ``value`` for a span attribute; never raises."""
    try:
        return json.dumps(plain(value), ensure_ascii=False, default=str)
    except Exception:
        return str(value)
