# -*- coding: utf-8 -*-
"""HoloScope read REST. HoloScope speaks the Langfuse v3 public API."""

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

_TRACES_PATH = "/api/public/traces"
_OBSERVATIONS_PATH = "/api/public/observations"
_SCORES_PATH = "/api/public/v2/scores"
_PAGE_LIMIT = 100
_SCORE_BATCH = 50
_ID_BATCH = 50
# The observation scan returns every step that ended in the window, not just
# roots; past this many per allowed root the window counts as too big.
_STEPS_PER_ROOT = 20


async def _pages(
    http: httpx.AsyncClient,
    path: str,
    params: dict[str, Any],
    cap: int | None = None,
    *,
    per_root: int = 1,
) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    page = 1
    while True:
        response = await http.get(
            path, params={**params, "page": page, "limit": _PAGE_LIMIT}
        )
        response.raise_for_status()
        body = response.json()
        items.extend(body.get("data") or [])
        if cap is not None and len(items) > cap * per_root:
            raise TooManyTraces(cap)
        if page >= int((body.get("meta") or {}).get("totalPages") or 1):
            return items
        page += 1


def _trace(trace: dict[str, Any]) -> SpanRow:
    """List rows carry no end time; ``list_roots`` fills it from the root step."""
    trace_id = str(trace.get("id") or "")
    name = str(trace.get("name") or "")
    scores = trace.get("scores")
    return {
        "id": trace_id,
        "trace_id": trace_id,
        "name": name,
        "trace_name": name,
        "start_time": get_time(trace.get("timestamp")),
        "end_time": None,
        "parent_id": None,
        "session_id": str(trace.get("sessionId") or ""),
        "user_id": str(trace.get("userId") or ""),
        "metadata": read_metadata(trace.get("metadata")),
        "tags": read_tags(trace.get("tags")),
        "release": read_text(trace.get("release")),
        "input": None,
        "output": None,
        **read_observation(trace),
        "score_ids": [item for item in scores or () if isinstance(item, str)],
    }


def _span(trace: dict[str, Any], raw: dict[str, Any]) -> SpanRow:
    root = _trace(trace)
    parent_id = raw.get("parentObservationId")
    metadata = read_metadata(raw.get("metadata"))
    if parent_id is None:
        metadata = {**metadata, **root["metadata"]}
    return {
        **root,
        "id": str(raw.get("id") or ""),
        "name": str(raw.get("name") or ""),
        "start_time": get_time(raw.get("startTime")),
        "end_time": get_time(raw.get("endTime"), required=False),
        "parent_id": parent_id,
        "metadata": metadata,
        "input": raw.get("input"),
        "output": raw.get("output"),
        **read_observation(raw),
    }


def _scores(raws: list[dict[str, Any]]) -> list[ScoreRow]:
    return [
        score_row(raw, str(raw["traceId"]))
        for raw in raws
        if raw.get("traceId") and not raw.get("observationId")
    ]


def _where(where: RootFilter) -> dict[str, Any]:
    """v3 trace list params. No metadata or trace id filter there; the select
    layer applies them."""
    params: dict[str, Any] = {
        "sessionId": where.session_id,
        "userId": where.user_id,
        "release": where.release,
        "tags": list(where.tags) or None,
    }
    return {key: value for key, value in params.items() if value is not None}


def _ended_filter(
    kind: TraceName, where: RootFilter, window: TimeWindow
) -> list[dict[str, Any]]:
    """v3 observation filter: steps of ``kind`` traces ending in the window."""
    conditions: list[dict[str, Any]] = [
        {"type": "string", "column": "traceName", "operator": "=", "value": kind},
        {
            "type": "datetime",
            "column": "endTime",
            "operator": ">=",
            "value": iso(window.from_time),
        },
        {
            "type": "datetime",
            "column": "endTime",
            "operator": "<",
            "value": iso(window.to_time),
        },
    ]
    if where.tags:
        conditions.append(
            {
                "type": "arrayOptions",
                "column": "traceTags",
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
    return conditions


async def _ended_roots(
    http: httpx.AsyncClient,
    kind: TraceName,
    where: RootFilter,
    window: TimeWindow,
    cap: int,
) -> dict[str, dict[str, Any]]:
    """Root steps that ended in the window, by trace id. The observation list
    has no session id, so the session filter waits for ``_traces_by_id``."""
    params: dict[str, Any] = {"filter": json.dumps(_ended_filter(kind, where, window))}
    if where.user_id is not None:
        params["userId"] = where.user_id
    steps = await _pages(
        http, _OBSERVATIONS_PATH, params, cap, per_root=_STEPS_PER_ROOT
    )
    roots = {
        str(step["traceId"]): step
        for step in steps
        if step.get("traceId") and step.get("parentObservationId") is None
    }
    if len(roots) > cap:
        raise TooManyTraces(cap)
    return roots


async def _traces_by_id(
    http: httpx.AsyncClient,
    kind: TraceName,
    where: RootFilter,
    roots: dict[str, dict[str, Any]],
    window: TimeWindow,
) -> list[SpanRow]:
    if not roots:
        return []
    earliest = min(get_time(step.get("startTime")) for step in roots.values())
    rows: list[SpanRow] = []
    for batch in chunks(sorted(roots), _ID_BATCH):
        traces = await _pages(
            http,
            _TRACES_PATH,
            {
                **_where(where),
                "name": kind,
                "fromTimestamp": iso(earliest),
                "toTimestamp": iso(window.to_time),
                "fields": "core,io,scores",
                "filter": json.dumps(
                    [
                        {
                            "type": "stringOptions",
                            "column": "id",
                            "operator": "any of",
                            "value": batch,
                        }
                    ]
                ),
            },
        )
        rows.extend(
            {
                **_trace(trace),
                "end_time": get_time(roots[str(trace["id"])].get("endTime")),
            }
            for trace in traces
            if str(trace.get("id")) in roots
        )
    return rows


async def list_roots(
    http: httpx.AsyncClient,
    kind: TraceName,
    where: RootFilter,
    window: TimeWindow,
    cap: int,
) -> list[SpanRow]:
    """A start window reads the trace list directly. An end window takes two
    steps, since the trace list only filters on start: root steps ending in
    the window, then their traces by id."""
    if window.field == "end_time":
        roots = await _ended_roots(http, kind, where, window, cap)
        return await _traces_by_id(http, kind, where, roots, window)
    traces = await _pages(
        http,
        _TRACES_PATH,
        {
            **_where(where),
            "name": kind,
            "fromTimestamp": iso(window.from_time),
            "toTimestamp": iso(window.to_time),
            "orderBy": "timestamp.asc",
            "fields": "core,io,scores",
        },
        cap,
    )
    return [_trace(trace) for trace in traces]


async def list_scores(http: httpx.AsyncClient, roots: list[SpanRow]) -> list[ScoreRow]:
    score_ids = list(
        dict.fromkeys(item for row in roots for item in row.get("score_ids") or ())
    )
    raws: list[dict[str, Any]] = []
    for batch in chunks(score_ids, _SCORE_BATCH):
        raws.extend(await _pages(http, _SCORES_PATH, {"scoreIds": ",".join(batch)}))
    return _scores(raws)


async def list_session_scores(
    http: httpx.AsyncClient, session_ids: list[str]
) -> dict[str, list[ScoreRow]]:
    """One request per session: v2 scores takes a single ``sessionId``."""
    grouped: dict[str, list[ScoreRow]] = {}
    for session_id in dict.fromkeys(session_ids):
        raws = await _pages(http, _SCORES_PATH, {"sessionId": session_id})
        rows = [
            score_row(raw, "")
            for raw in raws
            if raw.get("sessionId") == session_id and not raw.get("traceId")
        ]
        if rows:
            grouped[session_id] = rows
    return grouped


async def get_trace(
    http: httpx.AsyncClient,
    trace_id: str,
    *,
    parts: frozenset[SpanPart],
    with_scores: bool,
) -> tuple[list[SpanRow], list[ScoreRow]]:
    """The trace detail endpoint always returns every part; the caller trims."""
    del parts
    response = await http.get(f"{_TRACES_PATH}/{trace_id}")
    if response.status_code == 404:
        return [], []
    response.raise_for_status()
    trace = response.json()
    observations = trace.get("observations") or []
    spans = [_span(trace, raw) for raw in observations] or [_trace(trace)]
    scores = _scores(trace.get("scores") or []) if with_scores else []
    return spans, scores
