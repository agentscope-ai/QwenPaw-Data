# -*- coding: utf-8 -*-
"""Chat reads: one reply turn, root trace name ``chat``."""

from __future__ import annotations

from datetime import datetime

from qwenpaw_data.tracing.core.context.ids import check_trace_id
from qwenpaw_data.tracing.core.names import TraceName
from qwenpaw_data.tracing.query.backend import client, get_backend
from qwenpaw_data.tracing.query.backend.contract import RootFilter, ScoreRow, SpanRow
from qwenpaw_data.tracing.query.domain.trace import gather_limited, require_root
from qwenpaw_data.tracing.query.models import (
    Chat,
    LabelFilter,
    MetadataField,
    MetadataPart,
    Score,
    Span,
    SpanField,
    SpanPart,
)
from qwenpaw_data.tracing.query.select.fields import (
    metadata_parts,
    pick_metadata,
    pick_span,
    span_parts,
)
from qwenpaw_data.tracing.query.select.labels import group_scores
from qwenpaw_data.tracing.query.select.window import (
    Order,
    by_end,
    check_order,
    check_session_id,
    fetch_roots,
    list_query,
    ordered,
)


def _to_spans(
    rows: list[SpanRow],
    parts: frozenset[SpanPart],
    metadata: frozenset[MetadataPart],
) -> list[Span]:
    spans: list[Span] = []
    for row in sorted(rows, key=lambda item: (item["start_time"], item["id"])):
        picked = pick_span(row, parts)
        spans.append(
            Span(
                id=row["id"],
                name=row["name"],
                start_time=row["start_time"],
                end_time=row["end_time"],
                parent_id=row["parent_id"],
                metadata=pick_metadata(row["metadata"], metadata),
                type=row["type"],
                level=row["level"],
                status_message=row["status_message"],
                latency=row["latency"],
                time_to_first_token=row["time_to_first_token"],
                **picked,
            )
        )
    return spans


def to_chat(
    root: SpanRow,
    scores: list[Score],
    spans: list[Span],
    metadata: frozenset[MetadataPart],
) -> Chat:
    return Chat(
        trace_id=root["trace_id"],
        session_id=root["session_id"],
        user_id=root["user_id"],
        started_at=root["start_time"],
        ended_at=root["end_time"],
        release=root["release"],
        metadata=pick_metadata(root["metadata"], metadata),
        scores=scores,
        spans=spans,
    )


def build_chat(
    root: SpanRow,
    spans: list[SpanRow],
    scores: list[ScoreRow],
    parts: frozenset[SpanPart],
    metadata: frozenset[MetadataPart],
) -> Chat:
    """A Chat with its spans, from one ``get_trace`` result."""
    return to_chat(
        root,
        group_scores(scores).get(root["trace_id"], []),
        _to_spans(spans, parts, metadata),
        metadata,
    )


async def tracer_list_chats(
    session_id: str,
    *,
    chat_trace_ids: list[str] | None = None,
    from_end_time: datetime | None = None,
    to_end_time: datetime | None = None,
    label_filters: list[LabelFilter] | None = None,
    label_match: str = "all",
    order: Order = "asc",
    limit: int | None = None,
    span_fields: SpanField | None = None,
    metadata_fields: MetadataField = MetadataField.BUSINESS,
) -> list[Chat]:
    """Chats that ended in the window, by end time; ``limit`` keeps the first N
    in ``order``. A Chat still running is not readable yet.

    No spans unless ``span_fields`` is given; that costs one trace read per Chat.
    """
    check_order(order, limit)
    query = list_query(
        TraceName.CHAT,
        RootFilter(session_id=check_session_id(session_id)),
        chat_trace_ids,
        "end_time",
        from_end_time,
        to_end_time,
        label_filters,
        label_match,
    )
    parts = span_parts(span_fields) if span_fields is not None else None
    metadata = metadata_parts(metadata_fields)
    settings, backend = get_backend()
    async with client(settings) as http:
        rows, scores = await fetch_roots(http, backend, query)
        rows = ordered(rows, by_end, order, limit)
        if parts is None:
            return [
                to_chat(row, scores.get(row["trace_id"], []), [], metadata)
                for row in rows
            ]
        traces = await gather_limited(
            backend.get_trace(http, row["trace_id"], parts=parts, with_scores=False)
            for row in rows
        )
    return [
        to_chat(
            row,
            scores.get(row["trace_id"], []),
            _to_spans(spans, parts, metadata),
            metadata,
        )
        for row, (spans, _) in zip(rows, traces, strict=True)
    ]


async def tracer_get_chat(
    trace_id: str,
    *,
    session_id: str | None = None,
    span_fields: SpanField = SpanField.IO,
    metadata_fields: MetadataField = MetadataField.BUSINESS,
) -> Chat:
    tid = check_trace_id(trace_id)
    sid = check_session_id(session_id) if session_id is not None else None
    parts = span_parts(span_fields)
    metadata = metadata_parts(metadata_fields)
    settings, backend = get_backend()
    async with client(settings) as http:
        spans, scores = await backend.get_trace(
            http, tid, parts=parts, with_scores=True
        )
    root = require_root(spans, TraceName.CHAT, tid, sid)
    return build_chat(root, spans, scores, parts, metadata)
