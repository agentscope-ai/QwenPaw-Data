# -*- coding: utf-8 -*-
"""Host trace metadata: datasource / parent session / source_trace."""

from __future__ import annotations

import json


def trace_metadata(
    *,
    datasource_id: str | None = None,
    dataset_names: list | None = None,
    parent_session_id: str | None = None,
    source_trace_id: str | None = None,
) -> str | None:
    """Caller's trace metadata keys, JSON-packed; None when all empty."""
    payload: dict = {}
    if isinstance(datasource_id, str) and datasource_id.strip():
        payload["datasource_id"] = datasource_id.strip()
    if isinstance(dataset_names, list):
        names = [str(name) for name in dataset_names if str(name).strip()]
        if names:
            payload["dataset_names"] = names
    if isinstance(parent_session_id, str) and parent_session_id.strip():
        payload["parent_session_id"] = parent_session_id.strip()
    if isinstance(source_trace_id, str) and source_trace_id.strip():
        payload["source_trace_id"] = source_trace_id.strip()
    if not payload:
        return None
    return json.dumps(payload, ensure_ascii=False)
