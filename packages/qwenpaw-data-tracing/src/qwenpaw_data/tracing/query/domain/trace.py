# -*- coding: utf-8 -*-
"""Reading Chat / Task structure out of one trace's spans."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Iterable
from typing import TypeVar

from qwenpaw_data.tracing.core.names import Step, TraceName
from qwenpaw_data.tracing.query.backend.contract import SpanRow

FETCH_CONCURRENCY = 8
T = TypeVar("T")


async def gather_limited(calls: Iterable[Awaitable[T]]) -> list[T]:
    gate = asyncio.Semaphore(FETCH_CONCURRENCY)

    async def run(call: Awaitable[T]) -> T:
        async with gate:
            return await call

    return list(await asyncio.gather(*(run(call) for call in calls)))


def find_root(spans: list[SpanRow], kind: TraceName) -> SpanRow | None:
    roots = sorted(
        (row for row in spans if not row["parent_id"]),
        key=lambda row: (row["start_time"], row["id"]),
    )
    if not roots or roots[0]["trace_name"] != kind:
        return None
    return roots[0]


def require_root(
    spans: list[SpanRow], kind: TraceName, trace_id: str, session_id: str | None
) -> SpanRow:
    root = find_root(spans, kind)
    if root is None or (session_id is not None and root["session_id"] != session_id):
        raise LookupError(f"{kind} {trace_id} was not found")
    return root


def chat_ref_ids(spans: list[SpanRow]) -> list[str]:
    """Member Chat ids in the order the Task wrote them."""
    refs = sorted(
        (row for row in spans if row["name"] == Step.CHAT_REF and row["parent_id"]),
        key=lambda row: (row["start_time"], row["id"]),
    )
    ids: list[str] = []
    for row in refs:
        source = row["metadata"].get("source_trace_id")
        if isinstance(source, str) and source.strip():
            ids.append(source.strip())
    return list(dict.fromkeys(ids))
