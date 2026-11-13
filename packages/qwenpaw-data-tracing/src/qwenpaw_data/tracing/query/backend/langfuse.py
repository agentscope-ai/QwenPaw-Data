# -*- coding: utf-8 -*-
"""Langfuse v4 read REST: v2 observations + v3 scores."""

from __future__ import annotations

import json
from typing import Any

import httpx
from qwenpaw_data.tracing.core.names import TraceName
from qwenpaw_data.tracing.query.backend._parse import (
    chunks,
    get_time,
    iso,
    read_metadata,
    read_observation,
    read_tags,
    read_text,
    score_row,
)
from qwenpaw_data.tracing.query.backend.contract import (
    RootFilter,
    ScoreRow,
    SpanRow,
    TimeWindow,
    TooManyTraces,
)
from qwenpaw_data.tracing.query.models import SpanPart

_OBS_PATH = "/api/public/v2/observations"
_SCORE_PATH = "/api/public/v3/scores"
_BASE_FIELDS = ("core", "basic", "metadata", "trace_context", "metrics")
_PART_FIELDS = {
    SpanPart.INPUT: "io",
    SpanPart.OUTPUT: "io",
    SpanPart.MODEL: "model",
    SpanPart.USAGE: "usage",
    SpanPart.COST: "usage",
}
_TIME_COLUMNS = {"start_time": "startTime", "end_time": "endTime"}
_EXPAND = "desc"
_OBS_LIMIT = 1000
_SCORE_LIMIT = 100
_SCORE_BATCH = 50


def _fields(parts: frozenset[SpanPart]) -> str:
    groups = _BASE_FIELDS + tuple(g for p, g in _PART_FIELDS.items() if p in parts)
    return ",".join(dict.fromkeys(groups))


async def _items(
    http: httpx.AsyncClient,
    path: str,
    params: dict[str, Any],
    cap: int | None = None,
) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    cursor: str | None = None
    while True:
        request = dict(params)
        if cursor:
            request["cursor"] = cursor
        response = await http.get(path, params=request)
        response.raise_for_status()
        body = response.json()
        items.extend(body.get("data") or [])
        if cap is not None and len(items) > cap:
            raise TooManyTraces(cap)
        cursor = (body.get("meta") or {}).get("cursor")
        if not cursor:
            return items


def _io(value: object) -> object | None:
    """v4 hands IO back as its JSON text; text that was never JSON stays text."""
    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return value
    return value


def _span(raw: dict[str, Any]) -> SpanRow:
    return {
        "id": str(raw.get("id") or ""),
        "trace_id": str(raw.get("traceId") or ""),
        "name": str(raw.get("name") or ""),
        "trace_name": str(raw.get("traceName") or ""),
        "start_time": get_time(raw.get("startTime")),
        "end_time": get_time(raw.get("endTime"), required=False),
        "parent_id": raw.get("parentObservationId"),
        "session_id": str(raw.get("sessionId") or ""),
        "user_id": str(raw.get("userId") or ""),
        "metadata": read_metadata(raw.get("metadata")),
        "tags": read_tags(raw.get("tags")),
        "release": read_text(raw.get("release")),
        "input": _io(raw.get("input")),
        "output": _io(raw.get("output")),
        **read_observation(raw),
    }


def _where(where: RootFilter) -> list[dict[str, Any]]:
    conditions: list[dict[str, Any]] = []
    for column, value in (
        ("sessionId", where.session_id),
        ("userId", where.user_id),
        ("release", where.release),
    ):
        if value is not None:
            conditions.append(
                {"type": "string", "column": column, "operator": "=", "value": value}
            )
    if where.tags:
        conditions.append(
            {
                "type": "arrayOptions",
                "column": "tags",
                "operator": "all of",
                "value": list(where.tags),
            }
        )
    if where.trace_ids is not None:
        conditions.append(
            {
                "type": "stringOptions",
                "column": "traceId",
                "operator": "any of",
                "value": sorted(where.trace_ids),
            }
        )
    # Langfuse metadata filters only test one value; several are left to select.
    for item in where.metadata:
        if len(item.values) == 1:
            conditions.append(
                {
                    "type": "stringObject",
                    "column": "metadata",
                    "key": item.key,
                    "operator": "=",
                    "value": item.values[0],
                }
            )
    return conditions


async def list_roots(
    http: httpx.AsyncClient,
    kind: TraceName,
    where: RootFilter,
    window: TimeWindow,
    cap: int,
) -> list[SpanRow]:
    column = _TIME_COLUMNS[window.field]
    conditions = [
        *_where(where),
        {
            "type": "boolean",
            "column": "isRootObservation",
            "operator": "=",
            "value": True,
        },
        {"type": "string", "column": "traceName", "operator": "=", "value": kind},
        {
            "type": "datetime",
            "column": column,
            "operator": ">=",
            "value": iso(window.from_time),
        },
        {
            "type": "datetime",
            "column": column,
            "operator": "<",
            "value": iso(window.to_time),
        },
    ]
    rows = await _items(
        http,
        _OBS_PATH,
        {
            "filter": json.dumps(conditions),
            "fields": _fields(frozenset()),
            "expandMetadata": _EXPAND,
            "limit": _OBS_LIMIT,
        },
        cap,
    )
    return [_span(row) for row in rows]


async def _trace_scores(
    http: httpx.AsyncClient, trace_ids: list[str]
) -> list[ScoreRow]:
    rows: list[dict[str, Any]] = []
    for batch in chunks(list(dict.fromkeys(trace_ids)), _SCORE_BATCH):
        rows.extend(
            await _items(
                http,
                _SCORE_PATH,
                {
                    "traceId": ",".join(batch),
                    "fields": "details,subject",
                    "limit": _SCORE_LIMIT,
                },
            )
        )
    scores: list[ScoreRow] = []
    for raw in rows:
        subject = raw.get("subject") or {}
        if subject.get("kind") != "trace":
            continue
        scores.append(score_row(raw, str(subject.get("id") or "")))
    return scores


async def list_scores(http: httpx.AsyncClient, roots: list[SpanRow]) -> list[ScoreRow]:
    return await _trace_scores(http, [row["trace_id"] for row in roots])


async def list_session_scores(
    http: httpx.AsyncClient, session_ids: list[str]
) -> dict[str, list[ScoreRow]]:
    grouped: dict[str, list[ScoreRow]] = {}
    for batch in chunks(list(dict.fromkeys(session_ids)), _SCORE_BATCH):
        rows = await _items(
            http,
            _SCORE_PATH,
            {
                "sessionId": ",".join(batch),
                "fields": "details,subject",
                "limit": _SCORE_LIMIT,
            },
        )
        for raw in rows:
            subject = raw.get("subject") or {}
            if subject.get("kind") == "session":
                grouped.setdefault(str(subject.get("id") or ""), []).append(
                    score_row(raw, "")
                )
    return grouped


async def get_trace(
    http: httpx.AsyncClient,
    trace_id: str,
    *,
    parts: frozenset[SpanPart],
    with_scores: bool,
) -> tuple[list[SpanRow], list[ScoreRow]]:
    rows = await _items(
        http,
        _OBS_PATH,
        {
            "traceId": trace_id,
            "fields": _fields(parts),
            "expandMetadata": _EXPAND,
            "limit": _OBS_LIMIT,
        },
    )
    spans = [_span(row) for row in rows]
    if not spans or not with_scores:
        return spans, []
    return spans, await _trace_scores(http, [trace_id])
