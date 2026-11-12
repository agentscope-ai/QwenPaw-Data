"""Encode durable Undo values without changing temporal or missing-value semantics."""

from __future__ import annotations

import json
import math
from collections.abc import Mapping
from datetime import date, datetime


def _pack(value: object) -> object:
    if value is None or type(value) in (str, bool, int, float):
        return value
    if type(value) is datetime:
        return {"datetime": value.isoformat()}
    if type(value) is date:
        return {"date": value.isoformat()}
    if isinstance(value, Mapping):
        if any(type(key) is not str for key in value):
            raise TypeError("Undo mappings require text keys")
        # Tag every mapping so caller JSON cannot impersonate a temporal value.
        return {"mapping": [[key, _pack(item)] for key, item in value.items()]}
    if isinstance(value, (list, tuple)):
        return [_pack(item) for item in value]
    raise TypeError(f"unsupported Undo value: {type(value).__name__}")


def _unpack(value: object) -> object:
    if isinstance(value, list):
        return tuple(_unpack(item) for item in value)
    if isinstance(value, dict):
        if len(value) != 1:
            raise ValueError("invalid Undo value tag")
        kind, data = next(iter(value.items()))
        if kind == "datetime":
            return datetime.fromisoformat(data)
        if kind == "date":
            return date.fromisoformat(data)
        if kind == "mapping":
            result = {}
            for key, item in data:
                if type(key) is not str or key in result:
                    raise ValueError("invalid Undo mapping key")
                result[key] = _unpack(item)
            return result
        raise ValueError("unknown Undo value tag")
    if type(value) is float and not math.isfinite(value):
        raise ValueError("Undo cannot decode non-finite numbers")
    return value


def encode(value: object) -> str:
    return json.dumps(
        _pack(value), ensure_ascii=False, separators=(",", ":"), allow_nan=False
    )


def decode(payload: str) -> object:
    return _unpack(json.loads(payload))
