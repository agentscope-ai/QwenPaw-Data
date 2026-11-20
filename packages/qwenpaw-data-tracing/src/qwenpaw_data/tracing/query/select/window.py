# -*- coding: utf-8 -*-
"""List arguments, time window, picking the roots inside it, and ordering."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from typing import Any, Literal, TypeVar

import httpx
from qwenpaw_data.tracing.core.context.ids import check_trace_id
from qwenpaw_data.tracing.core.names import TraceName
from qwenpaw_data.tracing.query.backend.contract import (
    Backend,
    RootFilter,
    SpanRow,
    TimeField,
    TimeWindow,
)
from qwenpaw_data.tracing.query.models import LabelFilter, Score
from qwenpaw_data.tracing.query.select.labels import (
    check_label_filters,
    group_scores,
    match_labels,
)

MAX_ROOTS = 5000
LOOKBACK = timedelta(days=7)
AHEAD = timedelta(hours=1)

Order = Literal["asc", "desc"]
T = TypeVar("T")


@dataclass(frozen=True)
class ListQuery:
    kind: TraceName
    where: RootFilter
    window: TimeWindow
    label_filters: list[LabelFilter]
    label_match: str


def check_session_id(session_id: object) -> str:
    if not isinstance(session_id, str) or not session_id.strip():
        raise ValueError("session_id is required")
    return session_id.strip()


def check_order(order: object, limit: object) -> None:
    if order not in ("asc", "desc"):
        raise ValueError("order must be asc or desc")
    if limit is not None and (
        not isinstance(limit, int) or isinstance(limit, bool) or limit < 1
    ):
        raise ValueError("limit must be a positive integer")


def _check_time(value: datetime | None, name: str) -> datetime | None:
    if value is not None and value.tzinfo is None:
        raise ValueError(f"{name} must be timezone-aware")
    return value


def list_query(
    kind: TraceName,
    where: RootFilter,
    ids: list[str] | None,
    field: TimeField,
    from_time: datetime | None,
    to_time: datetime | None,
    label_filters: list[LabelFilter] | None = None,
    label_match: str = "all",
) -> ListQuery:
    """``from_time`` / ``to_time`` are the caller's ``from_<field>`` /
    ``to_<field>``; errors name them that way."""
    if ids is not None:
        where = replace(where, trace_ids=frozenset(check_trace_id(i) for i in ids))
    filters = check_label_filters(label_filters, label_match)
    start = _check_time(from_time, f"from_{field}")
    end = _check_time(to_time, f"to_{field}")
    now = datetime.now(UTC)
    return ListQuery(
        kind=kind,
        where=where,
        window=TimeWindow(field, start or (now - LOOKBACK), end or (now + AHEAD)),
        label_filters=filters,
        label_match=label_match,
    )


def _matches(row: SpanRow, where: RootFilter) -> bool:
    return (
        where.session_id in (None, row["session_id"])
        and where.user_id in (None, row["user_id"])
        and where.release in (None, row["release"])
        and set(where.tags) <= set(row["tags"])
        and all(row["metadata"].get(item.key) in item.values for item in where.metadata)
        and (where.trace_ids is None or row["trace_id"] in where.trace_ids)
    )


def _select_roots(rows: list[SpanRow], query: ListQuery) -> list[SpanRow]:
    """Exact kind / filter / window / ids, ordered by start time."""
    picked = [
        row
        for row in rows
        if row["trace_name"] == query.kind
        and row["session_id"]
        and _matches(row, query.where)
        and query.window.contains(row)
    ]
    picked.sort(key=by_start)
    return picked


def by_start(row: SpanRow) -> tuple[datetime, str]:
    return row["start_time"], row["trace_id"]


def by_end(row: SpanRow) -> tuple[datetime, str]:
    """Only for rows from an end window, which all have an end time."""
    return row["end_time"] or row["start_time"], row["trace_id"]


async def read_roots(
    http: httpx.AsyncClient, backend: Backend, query: ListQuery
) -> list[SpanRow]:
    """Every root in the window, without scores and label filters."""
    roots = await backend.list_roots(
        http, query.kind, query.where, query.window, MAX_ROOTS
    )
    return _select_roots(roots, query)


async def fetch_roots(
    http: httpx.AsyncClient, backend: Backend, query: ListQuery
) -> tuple[list[SpanRow], dict[str, list[Score]]]:
    """Every root in the window that passes the label filters, with its scores."""
    rows = await read_roots(http, backend, query)
    scores = group_scores(await backend.list_scores(http, rows))
    if query.label_filters:
        rows = [
            row
            for row in rows
            if match_labels(
                scores.get(row["trace_id"], []), query.label_filters, query.label_match
            )
        ]
    return rows, scores


def ordered(
    items: list[T], key: Callable[[T], Any], order: Order, limit: int | None
) -> list[T]:
    """Sort by ``key`` in ``order``, then keep the first ``limit``."""
    picked = sorted(items, key=key, reverse=order == "desc")
    return picked if limit is None else picked[:limit]
