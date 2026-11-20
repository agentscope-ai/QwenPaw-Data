# -*- coding: utf-8 -*-
"""Send and delete scores. Assembles ScoreEvent, then talks HTTP."""

from __future__ import annotations

import asyncio
import logging
from threading import Lock, Thread
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any
from weakref import WeakKeyDictionary

from qwenpaw_data.tracing.core.score import ScoreEvent
from qwenpaw_data.tracing.export.install import scores_endpoint
from qwenpaw_data.tracing.export.settings import Settings
from opentelemetry import trace
from opentelemetry.sdk.trace import ReadableSpan

logger = logging.getLogger(__name__)

_BATCH = 50
_DELAY = 2.0
_MAX_PENDING = 1000


@dataclass
class _SpanBatch:
    pending: list[dict] = field(default_factory=list)
    timer: asyncio.TimerHandle | None = None
    sender: asyncio.Task[None] | None = None
    dropped: int = 0


# Tools and shutdown run on the same service loop; do not mix loops or exporters.
_batches: WeakKeyDictionary = WeakKeyDictionary()
_Destination = tuple[str, tuple[str, str]]
# Producers without an event loop cannot schedule a timer. Keep one bounded
# process-wide backlog, with each score pinned to its original exporter.
_sync_pending: list[tuple[_Destination, dict]] = []
_sync_lock = Lock()


class ScoreResult(StrEnum):
    """Outcome of a score send or delete; only ``OK`` reached the backend."""

    OK = "ok"
    NOT_CONFIGURED = "not_configured"
    FAILED = "failed"


async def tracer_send_score_event(
    event: ScoreEvent, *, trace_id: str | None = None
) -> ScoreResult:
    return await _send(score_body(event, trace_id))


def tracer_send_span_score(
    name: str, value: float, span: ReadableSpan | trace.Span | None = None
) -> None:
    """Queue a numeric score on an explicit (possibly ended) or current span.

    A processor's on_end callback need not run with the tool as current span.
    """
    if span is None:
        span = trace.get_current_span()
    ctx = span.context if isinstance(span, ReadableSpan) else span.get_span_context()
    if ctx is None or not ctx.is_valid:
        return
    dest = _score_destination()
    if dest is None:
        return
    span_id = format(ctx.span_id, "016x")
    body = {
        "id": f"{name}:{span_id}",
        "traceId": format(ctx.trace_id, "032x"),
        "observationId": span_id,
        "name": name,
        "value": value,
        "dataType": "NUMERIC",
    }
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        with _sync_lock:
            if len(_sync_pending) < _MAX_PENDING:
                _sync_pending.append((dest, body))
        return
    states = _batches.setdefault(loop, {})
    state = states.setdefault(dest, _SpanBatch())
    if len(state.pending) >= _MAX_PENDING:
        state.dropped += 1
        return
    state.pending.append(body)
    if state.sender is None:
        if len(state.pending) >= _BATCH:
            _start_sender(state, dest)
        elif state.timer is None:
            state.timer = loop.call_later(_DELAY, _start_sender, state, dest)


def _start_sender(state: _SpanBatch, dest: tuple[str, tuple[str, str]]) -> None:
    if state.timer is not None:
        state.timer.cancel()
        state.timer = None
    if state.sender is None and state.pending:
        state.sender = asyncio.create_task(_drain(state, dest))


async def _drain(state: _SpanBatch, dest: tuple[str, tuple[str, str]]) -> None:
    import httpx

    url, auth = dest
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            while state.pending:
                batch = state.pending[:_BATCH]
                del state.pending[: len(batch)]
                try:
                    await post_ingestion_scores(client, url, auth, batch)
                except Exception:
                    logger.warning(
                        "score batch send failed n=%s", len(batch), exc_info=True
                    )
    finally:
        state.sender = None
        if state.dropped:
            logger.warning(
                "dropped %d span scores while exporter was busy", state.dropped
            )
            state.dropped = 0


def _take_sync_batches() -> dict[_Destination, _SpanBatch]:
    with _sync_lock:
        pending = _sync_pending[:]
        _sync_pending.clear()
    groups: dict[_Destination, _SpanBatch] = {}
    for dest, body in pending:
        if dest not in groups:
            groups[dest] = _SpanBatch()
        groups[dest].pending.append(body)
    return groups


async def _send_sync_batches(groups: dict[_Destination, _SpanBatch]) -> None:
    for dest, state in groups.items():
        await _drain(state, dest)


def flush_standalone_scores(timeout_millis: int) -> None:
    groups = _take_sync_batches()
    if not groups:
        return
    timeout = max(0, timeout_millis) / 1000

    def run() -> None:
        try:
            asyncio.run(asyncio.wait_for(_send_sync_batches(groups), timeout))
        except TimeoutError:
            logger.warning("standalone span score flush timed out")
        except Exception:
            logger.warning("standalone span score flush failed")

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        run()
    else:
        # A synchronous shutdown can also be called from an async embedding.
        # Never run a nested loop or take over that loop's own pending batches.
        worker = Thread(target=run, name="span-score-flush", daemon=True)
        worker.start()
        worker.join(timeout)


async def flush_span_scores() -> None:
    """Drain queued and in-flight batches before the owning service loop stops."""
    await _send_sync_batches(_take_sync_batches())
    states = _batches.get(asyncio.get_running_loop(), {})
    for dest, state in list(states.items()):
        _start_sender(state, dest)
        if state.sender is not None:
            await state.sender
        if not state.pending and state.sender is None:
            states.pop(dest, None)


async def _send(body: dict) -> ScoreResult:
    dest = _score_destination()
    if dest is None:
        return ScoreResult.NOT_CONFIGURED
    url, auth = dest
    try:
        import httpx

        async with httpx.AsyncClient(timeout=10.0) as client:
            await post_score(client, url, auth, body)
    except Exception:
        logger.warning(
            "score send failed id=%s name=%s",
            body.get("id"),
            body.get("name"),
            exc_info=True,
        )
        return ScoreResult.FAILED
    return ScoreResult.OK


async def tracer_send_score(
    *,
    id: str = "",
    name: str,
    value: str | float,
    chat_id: str = "",
    session_id: str | None = None,
    data_type: str = "CATEGORICAL",
    comment: str | None = None,
    metadata: dict[str, Any] | None = None,
    trace_id: str | None = None,
) -> ScoreResult:
    score_id = id or (f"{name}:{chat_id}" if chat_id else "")
    event = ScoreEvent(
        id=score_id,
        name=name,
        data_type=data_type,
        value=value,
        comment=comment,
        metadata=metadata,
        chat_id=chat_id,
        session_id=session_id,
    )
    return await tracer_send_score_event(event, trace_id=trace_id)


async def tracer_delete_score(*, id: str) -> ScoreResult:
    if not id:
        raise ValueError("score id is required")
    dest = _score_destination()
    if dest is None:
        return ScoreResult.NOT_CONFIGURED
    url, auth = dest
    try:
        import httpx

        async with httpx.AsyncClient(timeout=10.0) as client:
            await delete_score(client, url, auth, id)
    except Exception:
        logger.warning("score delete failed id=%s", id, exc_info=True)
        return ScoreResult.FAILED
    return ScoreResult.OK


def _score_destination() -> tuple[str, tuple[str, str]] | None:
    settings = Settings.from_env()
    if not settings.configured():
        return None
    return scores_endpoint(settings.host), (settings.public_key, settings.secret_key)


def score_body(row: Any, trace_id: str | None) -> dict:
    """Assemble the POST body from a delivery row or ``ScoreEvent``."""
    if row.data_type == "BOOLEAN":
        value: str | float | int = 1 if row.value_num else 0
    elif row.data_type == "NUMERIC":
        value = row.value_num if row.value_num is not None else 0
    else:
        value = row.value_text or ""
    body: dict = {
        "id": row.id,
        "name": row.name,
        "value": value,
        "dataType": row.data_type,
    }
    if trace_id:
        body["traceId"] = trace_id
    elif row.session_id:
        body["sessionId"] = row.session_id
    if row.comment:
        body["comment"] = row.comment
    metadata = row.metadata_json
    if isinstance(metadata, dict) and metadata:
        body["metadata"] = metadata
    return body


async def post_score(client: Any, url: str, auth: Any, body: dict) -> None:
    """POST one score; raises on any non-2xx outcome."""
    response = await client.post(url, json=body, auth=auth)
    response.raise_for_status()


def _ingestion_url(scores_url: str) -> str:
    return scores_url.removesuffix("/api/public/scores") + "/api/public/ingestion"


async def post_ingestion_scores(
    client: Any, scores_url: str, auth: Any, bodies: list[dict]
) -> None:
    """POST score-create events in one ingestion batch."""
    now = datetime.now(UTC).isoformat().replace("+00:00", "Z")
    payload = {
        "batch": [
            {
                "id": item["id"],
                "type": "score-create",
                "timestamp": now,
                "body": item,
            }
            for item in bodies
        ]
    }
    response = await client.post(_ingestion_url(scores_url), json=payload, auth=auth)
    if response.status_code == 207:
        errors = (response.json() or {}).get("errors") or []
        if errors:
            logger.warning("score batch partially rejected: %d errors", len(errors))
        return
    response.raise_for_status()


async def delete_score(client: Any, url: str, auth: Any, score_id: str) -> None:
    """DELETE one score; 404 means already gone, also fine."""
    response = await client.delete(f"{url}/{score_id}", auth=auth)
    if response.status_code not in {200, 202, 204, 404}:
        response.raise_for_status()


__all__ = [
    "ScoreResult",
    "delete_score",
    "flush_span_scores",
    "post_ingestion_scores",
    "post_score",
    "score_body",
    "tracer_delete_score",
    "tracer_send_score",
    "tracer_send_score_event",
    "tracer_send_span_score",
]
