# -*- coding: utf-8 -*-
from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict


class SpanPart(StrEnum):
    """Optional Span parts. Each value is also a ``Span`` / ``SpanRow`` field name.

    A new part also needs: that field on ``Span`` and ``SpanRow``, parsing in
    ``backend._parse.read_observation``, and its group in ``langfuse._PART_FIELDS``.
    """

    INPUT = "input"
    OUTPUT = "output"
    MODEL = "model"
    USAGE = "usage"
    COST = "cost"


class SpanField(StrEnum):
    BASIC = "basic"
    INPUT = "input"
    OUTPUT = "output"
    IO = "io"
    FULL = "full"


SPAN_FIELDS: dict[SpanField, frozenset[SpanPart]] = {
    SpanField.BASIC: frozenset(),
    SpanField.INPUT: frozenset({SpanPart.INPUT}),
    SpanField.OUTPUT: frozenset({SpanPart.OUTPUT}),
    SpanField.IO: frozenset({SpanPart.INPUT, SpanPart.OUTPUT}),
    SpanField.FULL: frozenset(SpanPart),
}
if set(SPAN_FIELDS) != set(SpanField):
    raise RuntimeError("every SpanField needs a SPAN_FIELDS entry")


class MetadataPart(StrEnum):
    BUSINESS = "business"
    PLATFORM = "platform"


class MetadataField(StrEnum):
    BUSINESS = "business"
    FULL = "full"


METADATA_FIELDS: dict[MetadataField, frozenset[MetadataPart]] = {
    MetadataField.BUSINESS: frozenset({MetadataPart.BUSINESS}),
    MetadataField.FULL: frozenset(MetadataPart),
}
if set(METADATA_FIELDS) != set(MetadataField):
    raise RuntimeError("every MetadataField needs a METADATA_FIELDS entry")


class LabelFilter(BaseModel):
    model_config = ConfigDict(frozen=True)

    name: str
    values: list[str]


class MetadataFilter(BaseModel):
    """Business metadata ``key`` equals one of ``values``."""

    model_config = ConfigDict(frozen=True)

    key: str
    values: list[str]


class Score(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: str
    name: str
    value: str | float
    data_type: str
    comment: str | None
    metadata: dict | None


class Span(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: str
    name: str
    start_time: datetime
    end_time: datetime | None
    parent_id: str | None
    metadata: dict
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


class Chat(BaseModel):
    model_config = ConfigDict(frozen=True)

    trace_id: str
    session_id: str
    user_id: str
    started_at: datetime
    ended_at: datetime | None
    release: str | None
    metadata: dict
    scores: list[Score]
    spans: list[Span]


class MissingChat(BaseModel):
    model_config = ConfigDict(frozen=True)

    trace_id: str
    reason: Literal["not_found", "session_mismatch"]


class Task(BaseModel):
    model_config = ConfigDict(frozen=True)

    trace_id: str
    session_id: str
    user_id: str
    started_at: datetime
    # Written by the Task writer as root metadata ``first_chat_at``; None when absent.
    first_chat_at: datetime | None
    metadata: dict
    chat_trace_ids: list[str]
    scores: list[Score]
    chats: list[Chat]
    missing_chats: list[MissingChat]


class Session(BaseModel):
    """Built from the Chats in the list window only, not the whole session."""

    model_config = ConfigDict(frozen=True)

    session_id: str
    user_id: str
    chat_count: int
    first_chat_at: datetime
    last_chat_at: datetime
    # Latest Chat end in the window.
    updated_at: datetime
    # Session-level scores; None unless asked for with ``with_scores``.
    scores: list[Score] | None
