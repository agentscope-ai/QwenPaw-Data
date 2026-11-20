# -*- coding: utf-8 -*-
from __future__ import annotations

import pytest
from qwenpaw_data.tracing.core.names import Score
from qwenpaw_data.tracing.runtime.host.scores import (
    tracer_score_followup_adopted,
    tracer_score_from_feedback,
)


def test_from_feedback_reaction() -> None:
    reaction = tracer_score_from_feedback(
        {"id": "fbk-1", "chat_id": "chat-1", "kind": "like", "reason": "good"}
    )
    assert reaction.name == Score.REACTION
    assert reaction.value == "like"
    assert reaction.comment == "good"


def test_from_feedback_rejects_unknown_kind() -> None:
    with pytest.raises(ValueError, match="unknown feedback kind"):
        tracer_score_from_feedback(
            {"id": "fbk-2", "chat_id": "chat-1", "kind": "unknown"}
        )


def test_from_feedback_copy_and_artifact_comment() -> None:
    copy = tracer_score_from_feedback(
        {"id": "fbk-c", "chat_id": "chat-1", "kind": "copy"}
    )
    assert copy.name == Score.COPY
    assert copy.data_type == "BOOLEAN"
    comment = tracer_score_from_feedback(
        {
            "id": "fbk-a",
            "chat_id": "chat-1",
            "kind": "artifact_comment",
            "detail": "note",
            "artifact_ref": {"path": "a.md", "line": 3},
        }
    )
    assert comment.name == Score.ARTIFACT_COMMENT
    assert comment.comment == "note\na.md:3"


def test_followup_adopted() -> None:
    adopted = tracer_score_followup_adopted(chat_id="chat-1", comment="next?")
    assert adopted.id == "followup:chat-1"
    assert adopted.name == Score.FOLLOWUP_ADOPTED
    assert adopted.value_text == "adopted"
