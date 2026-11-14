# -*- coding: utf-8 -*-
"""Chat trace: start this chat, or open a related root."""

from __future__ import annotations

import json
import logging
from collections.abc import Awaitable, Callable, Sequence
from contextlib import contextmanager
from typing import Any, Iterator, Self

from qwenpaw_data.tracing.core.context.ids import (
    bind_trace_id,
    new_trace_id,
    reset_bound_trace_id,
)
from qwenpaw_data.tracing.core.context.labels import (
    bind_trace_labels,
    bind_trace_tags,
    bound_trace_tags,
    empty_root_scope,
    merge_tags,
    put_tags,
    reset_trace_labels,
    reset_trace_tags,
    stamp_trace_labels,
)
from qwenpaw_data.tracing.core.context.ids import check_trace_id
from qwenpaw_data.tracing.core.names import Step, Tracer, TraceName
from qwenpaw_data.tracing.core.trace.handles import _RootSpan
from qwenpaw_data.tracing.core.trace.session import BaseSessionTracer
from qwenpaw_data.tracing.core.trace.tracer import BaseTracer, _step_name
from qwenpaw_data.tracing.runtime.common.propagation import (
    attach_incoming_trace,
    detach_incoming_trace,
)
from qwenpaw_data.tracing.runtime.host.chat_id import (
    bind_chat_trace,
    reset_chat_trace,
    set_chat_trace,
    trace_id_from_chat_id,
)
from qwenpaw_data.tracing.runtime.host.metadata import trace_metadata
from opentelemetry import trace

logger = logging.getLogger(__name__)

_RESERVED_METADATA = frozenset({"source_trace_id", "session_id", "user_id"})


class tracer_session(BaseSessionTracer):
    def __init__(self, user_id: str, session_id: str) -> None:
        if not isinstance(user_id, str) or not user_id.strip():
            raise ValueError("user_id is empty")
        if not isinstance(session_id, str) or not session_id.strip():
            raise ValueError("session_id is empty")
        super().__init__(user_id=user_id, session_id=session_id)


def _caller_metadata(metadata: dict | None) -> dict | None:
    if metadata is None:
        return None
    if not isinstance(metadata, dict):
        raise ValueError("metadata must be a dict")
    blocked = _RESERVED_METADATA.intersection(metadata)
    if blocked:
        raise ValueError("metadata must not contain " + ", ".join(sorted(blocked)))
    try:
        json.dumps(metadata, ensure_ascii=False)
    except (TypeError, ValueError) as exc:
        raise ValueError("metadata is not JSON serializable") from exc
    return dict(metadata) or None


class ChatTracer(BaseTracer):
    """One chat. ``with`` continues this chat; ``with`` plus ``step`` opens a related root."""

    TRACER_NAME = Tracer.HOST

    def __init__(
        self,
        session: tracer_session | None = None,
        *,
        metadata: dict | None = None,
        save_span_id: Callable[[str], Awaitable[None]] | None = None,
        attach_saved: Callable[[], Awaitable[object | None]] | None = None,
        chat_id: str | None = None,
        step: object | None = None,
        source_trace_id: str | None = None,
        headers: Any = None,
        tags: Sequence[str] | None = None,
    ) -> None:
        if session is not None and not isinstance(session, tracer_session):
            raise ValueError("session must come from tracer_session()")
        self._session = session
        self._metadata = _caller_metadata(metadata)
        self._save_span_id = save_span_id
        self._attach_saved = attach_saved
        self._chat_id = chat_id
        self._step = step
        self._source_trace_id = source_trace_id
        self._headers = headers
        self._tags = merge_tags(tags)
        self._span_id_stored = False
        self._root: _RootSpan | None = None
        self._labels: dict[str, str] | None = None
        self._chat_token: object | None = None
        self._saved_token: object | None = None
        self._header_token: object | None = None
        self._tags_token: object | None = None
        self._ended = False

    @property
    def trace_id(self) -> str | None:
        if self._root is not None and self._root._span is not None:
            ctx = self._root._span.get_span_context()
            if ctx.is_valid:
                return format(ctx.trace_id, "032x")
        if self._chat_id:
            return trace_id_from_chat_id(self._chat_id)
        return self._source_trace_id

    def _ensure_root(self) -> None:
        if self._headers is not None or self._step is None or self._root is not None:
            return
        if self._session is None:
            raise ValueError("session is required")
        self._source_trace_id = self.trace_id
        self._open_root(self._step)

    def _open_root(self, step: object) -> None:
        name = _step_name(step)
        metadata = trace_metadata(
            source_trace_id=self._source_trace_id, extra=self._metadata
        )
        labels = self._session.labels(metadata_json=metadata)
        labels["langfuse.trace.name"] = name
        self._labels = put_tags(labels, self._tags)
        bound = new_trace_id()
        bound_token = bind_trace_id(bound)
        chat_token = set_chat_trace(None)
        try:
            with empty_root_scope(labels):
                span = trace.get_tracer(self.TRACER_NAME).start_span(name)
        except Exception:
            logger.debug("new trace root skipped", exc_info=True)
            span = None
        finally:
            reset_chat_trace(chat_token)
            reset_bound_trace_id(bound_token)
        self._root = _RootSpan(span)

    def _enter_root(self) -> tuple[object, object | None] | None:
        if self._root is None:
            return None
        label_token = bind_trace_labels(self._labels or {})
        return (label_token, self._root.activate())

    def _exit_root(self, token: tuple[object, object | None] | None) -> None:
        if token is None or self._root is None:
            return
        label_token, ctx_token = token
        self._root.deactivate(ctx_token)
        reset_trace_labels(label_token)  # type: ignore[arg-type]

    def end(self, error: BaseException | str | None = None) -> None:
        if self._ended or self._root is None:
            return
        self._root.end(error)
        self._ended = True

    def __enter__(self) -> Self:
        self._ensure_root()
        self._sync_token = self._enter_root()
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        self._exit_root(getattr(self, "_sync_token", None))

    async def __aenter__(self) -> Self:
        if self._headers is not None:
            self._header_token = attach_incoming_trace(
                self._headers,
                session_header="x-cm-host-session-id",
                session_baggage_keys=("session.id", "langfuse.session.id"),
            )
            return self
        self._ensure_root()
        if self._root is not None:
            self._async_token = self._enter_root()
            return self
        if not self._chat_id:
            raise ValueError("chat_id is required")
        self._chat_token = bind_chat_trace(self._chat_id)
        if self._tags:
            self._tags_token = bind_trace_tags(self._tags)
        if self._attach_saved is not None:
            self._saved_token = await self._attach_saved()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: object,
    ) -> None:
        if self._header_token is not None:
            detach_incoming_trace(self._header_token)
            self._header_token = None
            return
        if self._root is not None:
            self._exit_root(getattr(self, "_async_token", None))
            self.end(exc if exc_type else None)
            return
        if self._saved_token is not None:
            from opentelemetry import context as otel_context

            otel_context.detach(self._saved_token)
            self._saved_token = None
        if self._tags_token is not None:
            reset_trace_tags(self._tags_token)  # type: ignore[arg-type]
            self._tags_token = None
        if self._chat_token is not None:
            reset_chat_trace(self._chat_token)  # type: ignore[arg-type]
            self._chat_token = None

    @contextmanager
    def set(self) -> Iterator[None]:
        """Stamp session labels on the current span and keep baggage attached."""
        if self._session is None:
            raise ValueError("session is required")
        labels = self._labels_for_chat()
        label_token = bind_trace_labels(labels)
        try:
            baggage_token = self._session.attach_baggage(labels)
            try:
                self._stamp_current_span(labels)
                yield
            finally:
                self._detach_baggage(baggage_token)
        finally:
            reset_trace_labels(label_token)

    def get(self, name: str = "span_id") -> str | None:
        if name != "span_id":
            raise ValueError(f"unknown tracer field: {name}")
        span_context = trace.get_current_span().get_span_context()
        if not span_context.is_valid:
            return None
        return format(span_context.span_id, "016x")

    async def set_span(self) -> None:
        if self._span_id_stored or self._save_span_id is None:
            return
        span_id = self.get()
        if not span_id:
            return
        await self._save_span_id(span_id)
        self._span_id_stored = True

    def _labels_for_chat(self) -> dict[str, str]:
        labels = self._session.labels(
            metadata_json=trace_metadata(extra=self._metadata)
        )
        labels["langfuse.trace.name"] = TraceName.CHAT
        return put_tags(labels, [*bound_trace_tags(), *self._tags])

    @staticmethod
    def _stamp_current_span(labels: dict[str, str]) -> None:
        span = trace.get_current_span()
        if not span.get_span_context().is_valid:
            return
        stamp_trace_labels(span, labels)

    @staticmethod
    def _detach_baggage(token: object) -> None:
        if token is None:
            return
        from opentelemetry import context

        context.detach(token)  # type: ignore[arg-type]


def tracer(
    session: tracer_session | None = None,
    *,
    chat_id: str | None = None,
    step: object | None = None,
    metadata: dict | None = None,
    headers: Any = None,
    attach_saved: Callable[[], Awaitable[object | None]] | None = None,
    save_span_id: Callable[[str], Awaitable[None]] | None = None,
    tags: Sequence[str] | None = None,
) -> ChatTracer:
    """Construct a tracer for ``with`` / ``async with``. The root opens on enter."""
    return ChatTracer(
        session,
        metadata=metadata,
        chat_id=chat_id,
        step=step,
        headers=headers,
        attach_saved=attach_saved,
        save_span_id=save_span_id,
        tags=tags,
    )


def tracer_start(
    session: tracer_session | None = None,
    *,
    chat_id: str | None = None,
    step: object | None = None,
    metadata: dict | None = None,
    headers: Any = None,
    attach_saved: Callable[[], Awaitable[object | None]] | None = None,
    save_span_id: Callable[[str], Awaitable[None]] | None = None,
    tags: Sequence[str] | None = None,
) -> ChatTracer:
    """Open now; pair with ``tracer_end``. Do not use with ``async with``."""
    started = tracer(
        session,
        chat_id=chat_id,
        step=step,
        headers=headers,
        attach_saved=attach_saved,
        save_span_id=save_span_id,
        tags=tags,
        metadata=metadata,
    )
    started._ensure_root()
    return started


def tracer_span(step: object, **kwargs: Any) -> Any:
    return ChatTracer.span(step, **kwargs)


def tracer_end(started: ChatTracer, error: BaseException | str | None = None) -> None:
    started.end(error)


def tracer_chat_ref(chat_trace_id: str) -> None:
    """Record one member Chat under the open Task root. Call once per Chat."""
    source = check_trace_id(chat_trace_id, "chat_trace_id")
    with ChatTracer.span(Step.CHAT_REF, metadata={"source_trace_id": source}):
        pass


__all__ = [
    "ChatTracer",
    "tracer",
    "tracer_chat_ref",
    "tracer_end",
    "tracer_session",
    "tracer_span",
    "tracer_start",
]
