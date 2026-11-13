# -*- coding: utf-8 -*-
"""Task reads: an RSI split of a session, root trace name ``task``."""

from __future__ import annotations

from datetime import datetime

import httpx
from qwenpaw_data.tracing.core.context.ids import check_trace_id
from qwenpaw_data.tracing.core.names import TaskMeta, TraceName
from qwenpaw_data.tracing.query.backend import client, get_backend
from qwenpaw_data.tracing.query.backend.contract import Backend, RootFilter, SpanRow
from qwenpaw_data.tracing.query.domain.chat import build_chat
from qwenpaw_data.tracing.query.domain.trace import (
    chat_ref_ids,
    find_root,
    gather_limited,
    require_root,
)
from qwenpaw_data.tracing.query.models import (
    Chat,
    LabelFilter,
    MetadataField,
    MetadataPart,
    MissingChat,
    Score,
    SpanField,
    SpanPart,
    Task,
)
from qwenpaw_data.tracing.query.select.fields import (
    metadata_parts,
    pick_metadata,
    span_parts,
)
from qwenpaw_data.tracing.query.select.labels import group_scores
from qwenpaw_data.tracing.query.select.window import (
    Order,
    check_order,
    check_session_id,
    fetch_roots,
    list_query,
    ordered,
)


def _first_chat_at(metadata: dict) -> datetime | None:
    """None when the writer left it out or wrote no timezone-aware ISO time."""
    value = metadata.get(TaskMeta.FIRST_CHAT_AT)
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


def to_task_summary(
    root: SpanRow,
    scores: list[Score],
    chat_trace_ids: list[str],
    metadata: frozenset[MetadataPart],
) -> Task:
    return Task(
        trace_id=root["trace_id"],
        session_id=root["session_id"],
        user_id=root["user_id"],
        started_at=root["start_time"],
        first_chat_at=_first_chat_at(root["metadata"]),
        metadata=pick_metadata(
            {k: v for k, v in root["metadata"].items() if k != TaskMeta.FIRST_CHAT_AT},
            metadata,
        ),
        chat_trace_ids=chat_trace_ids,
        scores=scores,
        chats=[],
        missing_chats=[],
    )


def with_members(task: Task, members: list[Chat | MissingChat]) -> Task:
    """``members`` in write order; found and missing are split, order kept."""
    return task.model_copy(
        update={
            "chats": [item for item in members if isinstance(item, Chat)],
            "missing_chats": [
                item for item in members if isinstance(item, MissingChat)
            ],
        }
    )


def _by_first_chat(row: SpanRow) -> tuple[datetime, str]:
    return _first_chat_at(row["metadata"]) or row["start_time"], row["trace_id"]


async def _read_member(
    http: httpx.AsyncClient,
    backend: Backend,
    trace_id: str,
    session_id: str,
    parts: frozenset[SpanPart],
    metadata: frozenset[MetadataPart],
) -> Chat | MissingChat:
    spans, scores = await backend.get_trace(
        http, trace_id, parts=parts, with_scores=True
    )
    root = find_root(spans, TraceName.CHAT)
    if root is None:
        return MissingChat(trace_id=trace_id, reason="not_found")
    if root["session_id"] != session_id:
        return MissingChat(trace_id=trace_id, reason="session_mismatch")
    return build_chat(root, spans, scores, parts, metadata)


async def _read_members(
    http: httpx.AsyncClient,
    backend: Backend,
    tasks: list[Task],
    parts: frozenset[SpanPart],
    metadata: frozenset[MetadataPart],
) -> list[list[Chat | MissingChat]]:
    """Every Task's members in one bounded batch, split back per Task."""
    flat = iter(
        await gather_limited(
            _read_member(http, backend, chat_id, task.session_id, parts, metadata)
            for task in tasks
            for chat_id in task.chat_trace_ids
        )
    )
    return [[next(flat) for _ in task.chat_trace_ids] for task in tasks]


async def tracer_list_tasks(
    session_id: str,
    *,
    task_trace_ids: list[str] | None = None,
    from_start_time: datetime | None = None,
    to_start_time: datetime | None = None,
    label_filters: list[LabelFilter] | None = None,
    label_match: str = "all",
    order: Order = "asc",
    limit: int | None = None,
    span_fields: SpanField | None = None,
    metadata_fields: MetadataField = MetadataField.BUSINESS,
) -> list[Task]:
    """Tasks whose own start is in the window, by ``first_chat_at``; ``limit``
    keeps the first N in ``order``. A Task without ``first_chat_at`` sorts by its
    own start.

    Member Chats only with ``span_fields``; otherwise just their ids.
    """
    check_order(order, limit)
    query = list_query(
        TraceName.TASK,
        RootFilter(session_id=check_session_id(session_id)),
        task_trace_ids,
        "start_time",
        from_start_time,
        to_start_time,
        label_filters,
        label_match,
    )
    parts = span_parts(span_fields) if span_fields is not None else None
    metadata = metadata_parts(metadata_fields)
    settings, backend = get_backend()
    async with client(settings) as http:
        rows, scores = await fetch_roots(http, backend, query)
        rows = ordered(rows, _by_first_chat, order, limit)
        traces = await gather_limited(
            backend.get_trace(
                http, row["trace_id"], parts=frozenset(), with_scores=False
            )
            for row in rows
        )
        tasks = [
            to_task_summary(
                row, scores.get(row["trace_id"], []), chat_ref_ids(spans), metadata
            )
            for row, (spans, _) in zip(rows, traces, strict=True)
        ]
        if parts is None:
            return tasks
        members = await _read_members(http, backend, tasks, parts, metadata)
    return [
        with_members(task, items) for task, items in zip(tasks, members, strict=True)
    ]


async def tracer_get_task(
    trace_id: str,
    *,
    session_id: str | None = None,
    span_fields: SpanField = SpanField.IO,
    metadata_fields: MetadataField = MetadataField.BUSINESS,
) -> Task:
    """Member Chats are read concurrently; unreadable ones land in ``missing_chats``."""
    tid = check_trace_id(trace_id)
    sid = check_session_id(session_id) if session_id is not None else None
    parts = span_parts(span_fields)
    metadata = metadata_parts(metadata_fields)
    settings, backend = get_backend()
    async with client(settings) as http:
        spans, scores = await backend.get_trace(
            http, tid, parts=frozenset(), with_scores=True
        )
        root = require_root(spans, TraceName.TASK, tid, sid)
        members = await gather_limited(
            _read_member(http, backend, chat_id, root["session_id"], parts, metadata)
            for chat_id in chat_ref_ids(spans)
        )
    task = to_task_summary(
        root,
        group_scores(scores).get(tid, []),
        [item.trace_id for item in members],
        metadata,
    )
    return with_members(task, members)
