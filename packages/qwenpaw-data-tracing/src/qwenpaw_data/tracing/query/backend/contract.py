# -*- coding: utf-8 -*-
"""What every backend hands the layers above: neutral rows, nothing else."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal, NotRequired, Protocol, TypedDict

import httpx
from qwenpaw_data.tracing.core.names import TraceName
from qwenpaw_data.tracing.query.models import MetadataFilter, SpanPart


class SpanRow(TypedDict):
    id: str
    trace_id: str
    name: str
    trace_name: str
    start_time: datetime
    end_time: datetime | None
    parent_id: str | None
    session_id: str
    user_id: str
    metadata: dict[str, Any]
    tags: list[str]
    release: str | None
    type: str | None
    level: str | None
    status_message: str | None
    latency: float | None
    time_to_first_token: float | None
    input: object | None
    output: object | None
    model: str | None
    usage: dict[str, int] | None
    cost: dict[str, float] | None
    # HoloScope only: list_scores looks scores up by these ids.
    score_ids: NotRequired[list[str]]


class ScoreRow(TypedDict):
    trace_id: str
    id: str
    name: str
    value: str | float
    data_type: str
    comment: str | None
    metadata: dict[str, Any] | None


@dataclass(frozen=True)
class RootFilter:
    """Root-span conditions. Backends push down what they can; the select layer
    re-checks every one, so a backend may also ignore some."""

    session_id: str | None = None
    user_id: str | None = None
    release: str | None = None
    tags: tuple[str, ...] = ()
    metadata: tuple[MetadataFilter, ...] = ()
    trace_ids: frozenset[str] | None = None


TimeField = Literal["start_time", "end_time"]


@dataclass(frozen=True)
class TimeWindow:
    """``[from_time, to_time)`` on the root's ``field``."""

    field: TimeField
    from_time: datetime
    to_time: datetime

    def contains(self, row: SpanRow) -> bool:
        value = row[self.field]
        return value is not None and self.from_time <= value < self.to_time


class TooManyTraces(ValueError):
    def __init__(self, cap: int) -> None:
        super().__init__(
            f"more than {cap} traces in the window; "
            "narrow the time window or add filters"
        )


class Backend(Protocol):
    async def list_roots(
        self,
        http: httpx.AsyncClient,
        kind: TraceName,
        where: RootFilter,
        window: TimeWindow,
        cap: int,
    ) -> list[SpanRow]: ...

    async def list_scores(
        self, http: httpx.AsyncClient, roots: list[SpanRow]
    ) -> list[ScoreRow]: ...

    async def list_session_scores(
        self, http: httpx.AsyncClient, session_ids: list[str]
    ) -> dict[str, list[ScoreRow]]:
        """Session-level scores by session id; their ``trace_id`` is empty."""
        ...

    async def get_trace(
        self,
        http: httpx.AsyncClient,
        trace_id: str,
        *,
        parts: frozenset[SpanPart],
        with_scores: bool,
    ) -> tuple[list[SpanRow], list[ScoreRow]]:
        """Empty spans when the trace does not exist."""
        ...
