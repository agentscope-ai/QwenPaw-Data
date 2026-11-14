# -*- coding: utf-8 -*-
"""Trace-label ContextVar. ChatTracer binds session labels from here."""

from __future__ import annotations

import json
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from contextvars import ContextVar, Token

from opentelemetry import context
from opentelemetry.context import Context
from opentelemetry.sdk.trace import Span

TAGS_KEY = "langfuse.trace.tags"
TAGS_MAX_LENGTH = 200

_trace_labels: ContextVar[dict[str, str] | None] = ContextVar(
    "qwenpaw_data_trace_labels",
    default=None,
)
_trace_tags: ContextVar[tuple[str, ...] | None] = ContextVar(
    "qwenpaw_data_trace_tags",
    default=None,
)


def merge_tags(tags: Sequence[str] | None) -> list[str]:
    """Deduplicate tags. Empty or over-long values fail immediately."""
    merged: list[str] = []
    for tag in tags or ():
        value = getattr(tag, "value", tag)
        if not isinstance(value, str):
            raise TypeError(f"tag must be a string, got {type(value).__name__}")
        if not value:
            raise ValueError("tag must not be empty")
        if len(value) > TAGS_MAX_LENGTH:
            raise ValueError(f"tag exceeds {TAGS_MAX_LENGTH} characters")
        if value not in merged:
            merged.append(value)
    return merged


def encode_tags(tags: Sequence[str]) -> str:
    return json.dumps(list(tags), ensure_ascii=False)


def parse_tags(value: str) -> list[str]:
    parsed = json.loads(value)
    if not isinstance(parsed, list) or any(
        not isinstance(item, str) for item in parsed
    ):
        raise ValueError("langfuse.trace.tags must be a JSON array of strings")
    return parsed


def put_tags(labels: dict[str, str], tags: Sequence[str] | None) -> dict[str, str]:
    """Write tags onto labels as JSON. Omit the key when the list is empty."""
    merged = merge_tags(tags)
    if merged:
        labels[TAGS_KEY] = encode_tags(merged)
    return labels


def stamp_tag_attribute(span: Span, value: str) -> None:
    """Decode baggage/label JSON and set ``langfuse.trace.tags`` as a string array."""
    tags = merge_tags(parse_tags(value))
    if tags:
        span.set_attribute(TAGS_KEY, tags)


def stamp_trace_labels(span: Span, labels: dict[str, str]) -> None:
    for key, value in labels.items():
        if key == TAGS_KEY:
            stamp_tag_attribute(span, value)
        else:
            span.set_attribute(key, value)


def bind_trace_labels(values: dict[str, str]) -> Token[dict[str, str] | None]:
    """Remember session and user for every span started until reset."""
    return _trace_labels.set(values)


def reset_trace_labels(token: Token[dict[str, str] | None]) -> None:
    _trace_labels.reset(token)


def bind_trace_tags(tags: Sequence[str]) -> Token[tuple[str, ...] | None]:
    """Remember caller tags for chat continuation until reset."""
    return _trace_tags.set(tuple(merge_tags(tags)))


def reset_trace_tags(token: Token[tuple[str, ...] | None]) -> None:
    _trace_tags.reset(token)


def bound_trace_tags() -> tuple[str, ...]:
    return _trace_tags.get() or ()


def apply_trace_labels(span: Span) -> None:
    """Write the bound labels onto this span, over a prior conversation id."""
    labels = _trace_labels.get()
    if not labels:
        return
    stamp_trace_labels(span, labels)


@contextmanager
def empty_root_scope(labels: dict[str, str]) -> Iterator[None]:
    """Start spans on a fresh root: empty OTEL context, bound session labels."""
    label_token = bind_trace_labels(labels)
    ctx_token = context.attach(Context())
    try:
        yield
    finally:
        context.detach(ctx_token)
        reset_trace_labels(label_token)
