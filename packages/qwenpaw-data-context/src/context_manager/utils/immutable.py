"""Detach nested container values into immutable snapshots."""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType
from typing import cast


def freeze_mapping(
    values: object,
    *,
    field_name: str,
    key_field_name: str,
) -> Mapping[str, object]:
    """Return one recursively detached mapping with immutable containers."""
    if not isinstance(values, Mapping):
        raise TypeError(f"{field_name} must be a mapping")
    return cast(
        Mapping[str, object], freeze_value(values, key_field_name=key_field_name)
    )


def freeze_value(value: object, *, key_field_name: str) -> object:
    """Detach nested containers into immutable snapshots; retain other values."""
    if type(value) in (float, int, str, bool, bytes, type(None)):
        return value
    if isinstance(value, Mapping):
        if any(not isinstance(name, str) for name in value):
            raise TypeError(f"{key_field_name} names must be text")
        return MappingProxyType(
            {
                name: freeze_value(item, key_field_name=key_field_name)
                for name, item in value.items()
            }
        )
    if isinstance(value, (list, tuple)):
        return tuple(
            freeze_value(item, key_field_name=key_field_name) for item in value
        )
    if isinstance(value, (set, frozenset)):
        return frozenset(
            freeze_value(item, key_field_name=key_field_name) for item in value
        )
    return value


__all__ = ["freeze_mapping", "freeze_value"]
