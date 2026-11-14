# -*- coding: utf-8 -*-
"""Host score assembly: feedback and followup adoption."""

from __future__ import annotations

from typing import Any

from qwenpaw_data.tracing.core.names import Score
from qwenpaw_data.tracing.core.score import ScoreEvent


def tracer_score_from_feedback(feedback: dict[str, Any]) -> ScoreEvent:
    kind = str(feedback["kind"])
    comment = _feedback_comment(feedback)
    chat_id = str(feedback["chat_id"])
    score_id = str(feedback["id"])
    if kind in {"like", "dislike"}:
        return ScoreEvent.update(
            id=score_id,
            name=Score.REACTION,
            value=kind,
            chat_id=chat_id,
            comment=comment,
        )
    if kind == "copy":
        return ScoreEvent.update(
            id=score_id,
            name=Score.COPY,
            value=1,
            chat_id=chat_id,
            data_type="BOOLEAN",
            comment=comment,
        )
    if kind == "artifact_comment":
        return ScoreEvent.update(
            id=score_id,
            name=Score.ARTIFACT_COMMENT,
            value="comment",
            chat_id=chat_id,
            comment=comment,
        )
    raise ValueError(f"unknown feedback kind: {kind}")


def tracer_score_followup_adopted(*, chat_id: str, comment: str) -> ScoreEvent:
    return ScoreEvent.update(
        id=f"followup:{chat_id}",
        name=Score.FOLLOWUP_ADOPTED,
        value="adopted",
        chat_id=chat_id,
        comment=comment,
    )


def _feedback_comment(feedback: dict[str, Any]) -> str | None:
    parts = [
        str(feedback.get("reason") or "").strip(),
        str(feedback.get("detail") or "").strip(),
    ]
    ref = feedback.get("artifact_ref")
    if isinstance(ref, dict):
        path = str(ref.get("path") or "")
        line = ref.get("line")
        if path:
            parts.append(f"{path}:{line}" if line is not None else path)
    text = "\n".join(part for part in parts if part)
    return text or None
