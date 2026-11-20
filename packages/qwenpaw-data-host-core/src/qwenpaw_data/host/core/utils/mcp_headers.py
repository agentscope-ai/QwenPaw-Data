# -*- coding: utf-8 -*-
from __future__ import annotations

import json

from qwenpaw_data.host.core.settings.mcp import McpSettings


def parse_mcp_headers(raw: str | None) -> dict[str, dict[str, str]]:
    header = McpSettings.HEADERS_HEADER
    if not raw or not raw.strip():
        return {}
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"{header} must be a JSON object") from exc
    if not isinstance(payload, dict):
        raise ValueError(f"{header} must be a JSON object")
    overlay: dict[str, dict[str, str]] = {}
    for name, headers in payload.items():
        if not isinstance(name, str) or not name.strip():
            raise ValueError(f"{header} MCP name is required")
        if not isinstance(headers, dict) or not all(
            isinstance(key, str) and isinstance(value, str)
            for key, value in headers.items()
        ):
            raise ValueError(f"{header} headers must be string pairs: {name}")
        overlay[name] = dict(headers)
    return overlay


def merge_mcp_headers(
    base: dict[str, dict[str, str]],
    overlay: dict[str, dict[str, str]],
) -> dict[str, dict[str, str]]:
    """Combine two overlays per MCP and per header; ``overlay`` wins.

    Header names are case-insensitive, so a differently-cased pair is the same
    header: the merged key is lowercased, the way the wire ends up anyway.
    """
    merged = {
        name: {key.lower(): value for key, value in values.items()}
        for name, values in base.items()
    }
    for name, values in overlay.items():
        target = merged.setdefault(name, {})
        for key, value in values.items():
            target[key.lower()] = value
    return merged
