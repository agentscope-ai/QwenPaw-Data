# -*- coding: utf-8 -*-
"""Host score and behavior writes."""

from __future__ import annotations

import logging
from typing import Any

from qwenpaw_data.host.core.db.tables import (
    ArtifactRow,
    BehaviorEventRow,
    ChatRow,
    FollowUpRow,
    ScoreDeliveryRow,
)
from qwenpaw_data.host.core.domain.identity import Identity
from qwenpaw_data.host.core.utils.ids import create_id
from qwenpaw_data.host.core.utils.time import utcnow
from qwenpaw_data.tracing.core.names import Score
from qwenpaw_data.tracing.core.score import ScoreEvent
from qwenpaw_data.tracing.runtime.host.scores import (
    tracer_score_followup_adopted,
    tracer_score_from_feedback,
)
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)


def normalize_question(text: str) -> str:
    return " ".join(text.split())


_SIGNAL_SCORE_NAMES = {
    "clarification": Score.CLARIFICATION,
    "steer": Score.STEER,
    "plan": Score.PLAN_EDIT,
}

_SIGNAL_COMMENT_FIELDS = {
    "clarification": "id",
    "steer": "text",
    "plan": "reason",
}

_METADATA_SCORE_NAMES = {Score.PLAN_EDIT}


def signal_score_name(command_kind: str) -> str | None:
    """Score name for a chat-command kind, or None when it emits no score."""
    return _SIGNAL_SCORE_NAMES.get(command_kind)


def signal_comment(command_kind: str, payload: dict[str, Any]) -> str | None:
    """Score comment for a chat-command kind, taken from its payload."""
    field = _SIGNAL_COMMENT_FIELDS.get(command_kind)
    if field is None:
        return None
    text = str(payload.get(field) or "").strip()
    return text or None


def _tracer_actor(
    target: Any,
    *,
    chat_id: str | None = None,
    session_id: str | None = None,
) -> tuple[Identity, str | None, str | None]:
    identity = getattr(target, "identity", None)
    if identity is not None:
        return (
            identity,
            target.id if chat_id is None else chat_id,
            session_id or target.session_id,
        )
    if not isinstance(target, Identity):
        raise ValueError("chat or identity is required")
    return target, chat_id, session_id


async def tracer_send_score(
    db: AsyncSession,
    identity: Identity,
    score: ScoreEvent | None = None,
    *,
    id: str = "",
    name: str = "",
    value: str | float | None = None,
    chat_id: str = "",
    session_id: str | None = None,
    data_type: str = "CATEGORICAL",
    comment: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> None:
    if score is None:
        score_id = id or (f"{name}:{chat_id}" if chat_id else "")
        score = ScoreEvent(
            id=score_id,
            name=name,
            data_type=data_type,
            value=value if value is not None else "",
            comment=comment,
            metadata=metadata,
            chat_id=chat_id,
            session_id=session_id,
        )
    existing = await db.get(ScoreDeliveryRow, score.id)
    if existing is not None:
        from qwenpaw_data.host.core.tracing.scores import MAX_DELIVERY_ATTEMPTS

        if existing.sent_at is not None and existing.attempts >= MAX_DELIVERY_ATTEMPTS:
            return
        changed = (
            existing.name != score.name
            or existing.data_type != score.data_type
            or existing.value_text != score.value_text
            or existing.value_num != score.value_num
        )
        if existing.sent_at is not None and not changed:
            return
        if existing.sent_at is not None and changed:
            existing.op = "replace"
            existing.sent_at = None
            existing.attempts = 0
        existing.chat_id = score.chat_id
        existing.session_id = score.session_id
        existing.name = score.name
        existing.data_type = score.data_type
        existing.value_text = score.value_text
        existing.value_num = score.value_num
        existing.comment = score.comment
        existing.metadata_json = dict(score.metadata) if score.metadata else None
        return
    db.add(
        ScoreDeliveryRow(
            id=score.id,
            user_id=identity.user_id,
            tenant_id=identity.tenant_id,
            workspace_id=identity.workspace_id,
            chat_id=score.chat_id,
            session_id=score.session_id,
            name=score.name,
            data_type=score.data_type,
            value_text=score.value_text,
            value_num=score.value_num,
            comment=score.comment,
            metadata_json=dict(score.metadata) if score.metadata else None,
            sent_at=None,
            created_at=utcnow(),
        )
    )


async def tracer_record_behavior(
    db: AsyncSession,
    target: Any,
    *,
    kind: str,
    session_id: str | None = None,
    chat_id: str | None = None,
    target_type: str = "",
    target_id: str = "",
    payload: dict[str, Any] | None = None,
    score_name: str | None = None,
    data_type: str = "CATEGORICAL",
    value: str | float | None = None,
    comment: str | None = None,
    score_on: str = "trace",
) -> str:
    identity, chat_id, session_id = _tracer_actor(
        target, chat_id=chat_id, session_id=session_id
    )
    try:
        now = utcnow()
        event_id = create_id("bev")
        db.add(
            BehaviorEventRow(
                id=event_id,
                user_id=identity.user_id,
                tenant_id=identity.tenant_id,
                workspace_id=identity.workspace_id,
                session_id=session_id,
                chat_id=chat_id,
                kind=kind,
                target_type=target_type,
                target_id=target_id,
                payload_json=dict(payload or {}),
                created_at=now,
            )
        )
        if score_name:
            metadata = (
                dict(payload)
                if score_name in _METADATA_SCORE_NAMES and payload
                else None
            )
            scored = value if value is not None else kind
            if score_on == "session":
                await tracer_send_score(
                    db,
                    identity,
                    ScoreEvent.create(
                        id=event_id,
                        name=score_name,
                        value=scored,
                        data_type=data_type,
                        comment=comment,
                        metadata=metadata,
                        session_id=session_id,
                    ),
                )
            elif chat_id:
                await tracer_send_score(
                    db,
                    identity,
                    ScoreEvent.create(
                        id=event_id,
                        name=score_name,
                        value=scored,
                        data_type=data_type,
                        comment=comment,
                        metadata=metadata,
                        chat_id=chat_id,
                    ),
                )
        await db.flush()
    except Exception:
        logger.warning(
            "behavior record failed kind=%s session=%s",
            kind,
            session_id,
            exc_info=True,
        )
        return ""
    return event_id


async def tracer_record_console(
    db: AsyncSession,
    identity: Identity,
    *,
    kind: str,
    session_id: str,
    chat_id: str | None = None,
    target_id: str = "",
    payload: dict[str, Any] | None = None,
) -> str:
    payload = dict(payload or {})
    return await tracer_record_behavior(
        db,
        identity,
        session_id=session_id,
        chat_id=chat_id,
        kind=kind,
        target_type=kind,
        target_id=target_id,
        payload=payload,
        score_name=kind,
        value=_console_value(kind, payload),
        comment=_console_comment(kind, payload, target_id),
        score_on=_console_score_on(kind, chat_id),
    )


async def tracer_send_status(
    db: AsyncSession,
    chat: Any,
    outcome: str | None = None,
    failure: Any = None,
) -> None:
    chat_id = chat.id
    if outcome is None:
        outcome = chat.status
        code = None
        if outcome == "failed" and isinstance(chat.error, dict):
            raw = chat.error.get("code")
            code = str(raw) if raw else None
    else:
        code = failure.code if outcome == "failed" and failure is not None else None
    await tracer_send_score(
        db,
        chat.identity,
        ScoreEvent.terminal(
            chat_id=chat_id,
            name=Score.CHAT_STATUS,
            value=outcome,
            metadata={"code": code} if code else None,
        ),
    )


async def tracer_send_stop(db: AsyncSession, chat: Any) -> None:
    await tracer_send_score(
        db,
        chat.identity,
        ScoreEvent.terminal(
            chat_id=chat.id,
            name=Score.STOP,
            value=1,
            data_type="BOOLEAN",
        ),
    )


async def tracer_send_confidence(
    db: AsyncSession,
    target: Any,
    segment: dict[str, Any],
    *,
    chat_id: str | None = None,
) -> None:
    identity, chat_id, _ = _tracer_actor(target, chat_id=chat_id)
    if not chat_id:
        raise ValueError("chat_id is required")
    confidence = segment.get("confidence")
    if not isinstance(confidence, dict):
        return
    level = confidence.get("level")
    if not isinstance(level, str) or not level.strip():
        return
    segment_id = segment.get("segment_id")
    if not isinstance(segment_id, str) or not segment_id:
        return
    summary = confidence.get("summary")
    await tracer_send_score(
        db,
        identity,
        ScoreEvent.update(
            id=f"{chat_id}:{segment_id}",
            name=Score.CONFIDENCE,
            value=level.strip(),
            chat_id=chat_id,
            comment=summary if isinstance(summary, str) and summary else None,
        ),
    )


async def tracer_send_feedback(
    db: AsyncSession, identity: Identity, feedback: dict[str, Any]
) -> None:
    await tracer_send_score(db, identity, tracer_score_from_feedback(feedback))


async def tracer_send_followup_adoption(db: AsyncSession, chat: Any) -> None:
    if chat.sequence < 1:
        return
    previous_id = await db.scalar(
        select(ChatRow.id).where(
            ChatRow.session_id == chat.session_id,
            ChatRow.sequence == chat.sequence - 1,
        )
    )
    if not previous_id:
        return
    questions = await db.scalar(
        select(FollowUpRow.questions_json).where(FollowUpRow.chat_id == previous_id)
    )
    if not questions:
        return
    incoming = normalize_question(chat.user_input)
    matched = next(
        (item for item in questions if normalize_question(str(item)) == incoming),
        None,
    )
    if matched is None:
        return
    await tracer_send_score(
        db,
        chat.identity,
        tracer_score_followup_adopted(chat_id=previous_id, comment=str(matched)),
    )


async def tracer_clear_reaction(
    db: AsyncSession, identity: Identity, score_id: str
) -> None:
    row = await db.get(ScoreDeliveryRow, score_id)
    if row is None:
        return
    if row.sent_at is None:
        await db.delete(row)
        return
    row.op = "delete"
    row.sent_at = None


def _console_value(kind: str, payload: dict[str, Any]) -> str:
    if kind == Score.TRACE_VIEW:
        view = payload.get("view")
        return str(view) if view else kind
    if kind == Score.SEGMENT_TOGGLE:
        return "open" if payload.get("open") else "closed"
    return kind


def _console_score_on(kind: str, chat_id: str | None) -> str:
    if kind == Score.SESSION_REVISIT:
        return "session"
    if kind == Score.TRACE_VIEW and not chat_id:
        return "session"
    return "trace"


def _console_comment(kind: str, payload: dict[str, Any], target_id: str) -> str | None:
    if kind == Score.SEGMENT_TOGGLE and target_id:
        state = "open" if payload.get("open") else "closed"
        return f"{target_id}:{state}"
    if kind == Score.TRACE_VIEW:
        view = payload.get("view")
        return str(view) if view else None
    return None


async def artifact_chat_id(
    db: AsyncSession, identity: Identity, session_id: str, path: str
) -> str | None:
    return await db.scalar(
        select(ArtifactRow.chat_id).where(
            ArtifactRow.user_id == identity.user_id,
            ArtifactRow.tenant_id == identity.tenant_id,
            ArtifactRow.workspace_id == identity.workspace_id,
            ArtifactRow.session_id == session_id,
            ArtifactRow.path == path,
        )
    )


__all__ = [
    "artifact_chat_id",
    "normalize_question",
    "signal_comment",
    "signal_score_name",
    "tracer_clear_reaction",
    "tracer_record_behavior",
    "tracer_record_console",
    "tracer_send_confidence",
    "tracer_send_feedback",
    "tracer_send_followup_adoption",
    "tracer_send_score",
    "tracer_send_status",
    "tracer_send_stop",
]
