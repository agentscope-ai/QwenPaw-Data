# -*- coding: utf-8 -*-
"""Session reads: Chat roots in the window, grouped by session id."""

from __future__ import annotations

from datetime import datetime

from qwenpaw_data.tracing.core.names import TraceName
from qwenpaw_data.tracing.query.backend import client, get_backend
from qwenpaw_data.tracing.query.backend.contract import RootFilter, SpanRow
from qwenpaw_data.tracing.query.models import LabelFilter, MetadataFilter, Session
from qwenpaw_data.tracing.query.select.labels import (
    check_label_filters,
    match_labels,
    to_score,
)
from qwenpaw_data.tracing.query.select.window import (
    Order,
    by_end,
    check_order,
    list_query,
    ordered,
    read_roots,
)


def _text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")
    return value.strip()


def _metadata_filter(item: MetadataFilter) -> MetadataFilter:
    if not item.values:
        raise ValueError("metadata filter values are required")
    return MetadataFilter(
        key=_text(item.key, "metadata filter key"),
        values=[_text(value, "metadata filter value") for value in item.values],
    )


def _root_filter(
    user_id: str | None,
    release: str | None,
    tags: list[str] | None,
    metadata_filters: list[MetadataFilter] | None,
) -> RootFilter:
    return RootFilter(
        user_id=None if user_id is None else _text(user_id, "user_id"),
        release=None if release is None else _text(release, "release"),
        tags=tuple(_text(tag, "tag") for tag in tags or ()),
        metadata=tuple(_metadata_filter(item) for item in metadata_filters or ()),
    )


def _to_session(chats: list[SpanRow]) -> Session:
    """``chats`` by start time, oldest first; all ended in the window."""
    last = chats[-1]
    return Session(
        session_id=last["session_id"],
        user_id=last["user_id"],
        chat_count=len(chats),
        first_chat_at=chats[0]["start_time"],
        last_chat_at=last["start_time"],
        updated_at=max(by_end(row)[0] for row in chats),
        scores=None,
    )


def _by_update(session: Session) -> tuple[datetime, str]:
    return session.updated_at, session.session_id


async def tracer_list_sessions(
    *,
    user_id: str | None = None,
    release: str | None = None,
    tags: list[str] | None = None,
    metadata_filters: list[MetadataFilter] | None = None,
    from_end_time: datetime | None = None,
    to_end_time: datetime | None = None,
    label_filters: list[LabelFilter] | None = None,
    label_match: str = "all",
    with_scores: bool = False,
    order: Order = "desc",
    limit: int | None = None,
) -> list[Session]:
    """Sessions with a Chat ending in the window, by ``updated_at``; ``limit``
    keeps the first N in ``order``.

    Filters match Chat roots and must all hold: all given ``tags`` present, and
    each ``metadata_filters`` key (business metadata, e.g. ``workspace_id``)
    equal to one of its values. ``label_filters`` match session-level scores.
    ``scores`` is filled only with ``with_scores``.
    """
    check_order(order, limit)
    filters = check_label_filters(label_filters, label_match)
    query = list_query(
        TraceName.CHAT,
        _root_filter(user_id, release, tags, metadata_filters),
        None,
        "end_time",
        from_end_time,
        to_end_time,
    )
    settings, backend = get_backend()
    async with client(settings) as http:
        rows = await read_roots(http, backend, query)
        grouped: dict[str, list[SpanRow]] = {}
        for row in rows:
            grouped.setdefault(row["session_id"], []).append(row)
        sessions = [_to_session(chats) for chats in grouped.values()]
        if not (filters or with_scores):
            return ordered(sessions, _by_update, order, limit)
        raw = await backend.list_session_scores(http, list(grouped))
    scores = {sid: [to_score(row) for row in items] for sid, items in raw.items()}
    if filters:
        sessions = [
            item
            for item in sessions
            if match_labels(scores.get(item.session_id, []), filters, label_match)
        ]
    if with_scores:
        sessions = [
            item.model_copy(update={"scores": scores.get(item.session_id, [])})
            for item in sessions
        ]
    return ordered(sessions, _by_update, order, limit)
