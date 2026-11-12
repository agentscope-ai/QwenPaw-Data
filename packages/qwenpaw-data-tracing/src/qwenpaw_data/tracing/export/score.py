# -*- coding: utf-8 -*-
"""Send and delete scores. Assembles ScoreEvent, then talks HTTP."""

from __future__ import annotations

import logging
from typing import Any

from qwenpaw_data.tracing.core.score import ScoreEvent
from qwenpaw_data.tracing.export.install import scores_endpoint
from qwenpaw_data.tracing.export.settings import Settings

logger = logging.getLogger(__name__)


async def tracer_send_score_event(
    event: ScoreEvent, *, trace_id: str | None = None
) -> None:
    dest = _score_destination()
    if dest is None:
        return
    url, auth = dest
    try:
        import httpx

        async with httpx.AsyncClient(timeout=10.0) as client:
            await post_score(client, url, auth, score_body(event, trace_id))
    except Exception:
        logger.warning(
            "score send failed id=%s name=%s", event.id, event.name, exc_info=True
        )


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
) -> None:
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
    await tracer_send_score_event(event, trace_id=trace_id)


async def tracer_delete_score(*, id: str) -> None:
    if not id:
        raise ValueError("score id is required")
    dest = _score_destination()
    if dest is None:
        return
    url, auth = dest
    try:
        import httpx

        async with httpx.AsyncClient(timeout=10.0) as client:
            await delete_score(client, url, auth, id)
    except Exception:
        logger.warning("score delete failed id=%s", id, exc_info=True)


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


async def delete_score(client: Any, url: str, auth: Any, score_id: str) -> None:
    """DELETE one score; 404 means already gone, also fine."""
    response = await client.delete(f"{url}/{score_id}", auth=auth)
    if response.status_code not in {200, 202, 204, 404}:
        response.raise_for_status()


__all__ = [
    "delete_score",
    "post_score",
    "score_body",
    "tracer_delete_score",
    "tracer_send_score",
    "tracer_send_score_event",
]
