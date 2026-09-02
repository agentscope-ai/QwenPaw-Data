"""Based on the frontend implementation of clarification question logic
"""
from __future__ import annotations

import datetime
import json
import logging
from dataclasses import dataclass, asdict
from typing import Any, Mapping

from qwenpaw_data.host.core.api.models.stream_objects import StreamObject

logger = logging.getLogger(__name__)

# ---- typed shapes (mirror of the TS interfaces in api/types.ts) ----

@dataclass
class ClarificationQuestionAnswer:
    label: str
    description: str | None


@dataclass
class ClarificationQuestion:
    question: str
    description: str
    multi_select: bool
    options: list[ClarificationQuestionAnswer]
    required: bool = True
    allow_custom_text: bool = True


@dataclass
class ClarificationQuestionGroup:
    id: str
    chat_id: str | None
    title: str
    questions: list[ClarificationQuestion]
    expires_at: str | None



# ---- helpers ----

def _parse_arguments(value: Any) -> Any:
    if not isinstance(value, str):
        return value
    try:
        return json.loads(value)
    except (ValueError, TypeError):
        return None


def _first_data_content(message: StreamObject) -> Mapping[str, Any] | None:
    """Return the first content item whose `type == 'data'`, else None."""
    content = getattr(message, "content", None)
    if not isinstance(content, list):
        logger.debug(f'first_data_content, content type:{type(content)}')
        return None
    for item in content:
        if isinstance(item, Mapping) and item.get("type") == "data":
            return item
        elif getattr(item, "type", None) == 'data':
            return item.model_dump()
    for item in content:
        logger.debug(f'first_data_content, item type:{type(item)}')
    return None


def _parse_ask_user_question(message: StreamObject) -> ClarificationQuestionGroup | None:
    """
    message example
    {
        "id":"msg_WMiXy8VN",
        "object":"message",
        "chat_id":"chat_63dvhmn4",
        "sequence":1,
        "type":"plugin_call",
        "role":"assistant",
        "content":[{
            "object":"content",
            "delta":false,
            "type":"data",
            "data":{
                "call_id":"call_2eeb9c534fff4ce0b1c1d326",
                "name":"ask_user_question",
                "arguments":"{
                    \"questions\": [{
                        \"multiSelect\": false,
                        \"options\": [
                            {\"description\": \"答案 A\", \"label\": \"81\"},
                            {\"description\": \"答案 B\", \"label\": \"91\"},
                            {\"description\": \"答案 C\", \"label\": \"72\"}
                        ],
                        \"question\": \"9² + √16 = ?\"
                    }],
                    \"title\": \"数学小测验\"
                }"
            }
        }],
        "status":"completed",
        "source_id":"call_2eeb9c534fff4ce0b1c1d326",
        "metadata":{"expires_at":"2026-08-19T00:14:10.040999Z"},
        "sequence_number":47,
        "session_id":"ses_NCcDBgKX"
    },
    """
    if getattr(message, "type", None) != "plugin_call" or getattr(message, "status", None) != "completed":
        logger.debug(f'parse user question 1, {message.model_dump()}')
        return None
    content = _first_data_content(message)
    data = content.get("data") if content else None
    if not isinstance(data, Mapping):
        logger.debug(f'parse user question 2, data, type:{type(data)}')
        return None
    if data.get("name") != "ask_user_question" or not isinstance(data.get("call_id"), str):
        logger.debug(f'parse user question 3, data, value:{data}')
        return None

    input_ = _parse_arguments(data.get("arguments"))
    if not isinstance(input_, Mapping):
        logger.debug(f'parse user question 4, input_, type:{type(input_)}')
        return None
    if not isinstance(input_.get("title"), str) or not isinstance(input_.get("questions"), list):
        logger.debug(f'parse user question 5, input_ title type:{type(input_.get("title"))}, '
                    f'questions type::{type(input_.get("questions"))}')
        return None

    raw_questions = input_["questions"]
    valid_questions: list[Mapping[str, Any]] = []
    for question in raw_questions:
        if not isinstance(question, Mapping):
            break
        if not isinstance(question.get("question"), str):
            break
        if question.get("description") is not None and not isinstance(question.get("description"), str):
            break
        if not isinstance(question.get("multiSelect"), bool):
            break
        options = question.get("options")
        if not isinstance(options, list) or len(options) < 1:
            break
        ok = True
        for option in options:
            if not isinstance(option, Mapping) or not isinstance(option.get("label"), str):
                ok = False
                break
            desc = option.get("description")
            if desc is not None and not isinstance(desc, str):
                ok = False
                break
        if not ok:
            break
        valid_questions.append(question)

    # every question must be valid (questions.length !== input.questions.length -> null)
    if len(valid_questions) != len(raw_questions) or not valid_questions:
        logger.debug(f'parse user question 6, valid:{len(valid_questions)}, raw:{len(raw_questions)}, '
                    f'valid:{type(valid_questions[0]) if valid_questions else None}, '
                    f'raw:{type(raw_questions[0]) if raw_questions else None}, ')
        return None

    normalized = [
        ClarificationQuestion(
            question=q["question"],
            description=q["description"] if isinstance(q.get("description"), str) else "",
            multi_select=q["multiSelect"],
            options=[
                ClarificationQuestionAnswer(
                    label=opt["label"],
                    description=opt["description"] if isinstance(opt.get("description"), str) else None,
                )
                for opt in q["options"]
            ],
        )
        for q in valid_questions
    ]

    metadata = getattr(message, "metadata", None)
    expires_at = (
        metadata.get("expires_at")
        if isinstance(metadata, Mapping) and isinstance(metadata.get("expires_at"), str)
        else None
    )
    chat_id = getattr(message, "chat_id", None)
    return ClarificationQuestionGroup(
        id=data["call_id"],
        chat_id=chat_id,
        title=input_["title"],
        questions=normalized,
        expires_at=expires_at,
    )


# ---- the requested selector ----

def build_clarification_questions(
    message: StreamObject,
) -> ClarificationQuestionGroup | None:
    """Return the most recent unresolved ask_user_question call, if any.
    """
    parsed = _parse_ask_user_question(message)
    if not parsed:
        return None
    if _is_ask_user_question_expired(parsed):
        logger.debug(f'an expired clarification question:{asdict(parsed)}')
        return None

    return parsed


# ---- expiry ----

def _parse_iso_to_epoch_ms(value: str) -> float | None:
    """Parse an ISO-8601 timestamp to epoch milliseconds.

    Mirrors JS `Date.parse`: returns None on any parse failure (caller treats
    None as "no deadline" rather than expired). Naive timestamps are assumed
    to be UTC, since `Date.parse` also resolves them against the local/UTC
    axis rather than rejecting them.
    """
    try:
        dt = datetime.datetime.fromisoformat(value)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=datetime.timezone.utc)
    return dt.timestamp() * 1000.0


def _is_ask_user_question_expired(
    clarification: ClarificationQuestionGroup,
    now: float | None = None,
) -> bool:
    """True if the clarification's `expires_at` deadline has passed.

    A missing or unparseable `expires_at` returns False (i.e. not expired),
    matching the TS semantics where `Date.parse` -> NaN short-circuits to
    "not expired". `now` defaults to the current wall clock and, when given,
    is interpreted as epoch milliseconds — the same units `Date.now()` uses —
    so callers can inject a deterministic timestamp in tests.
    """
    if not clarification.expires_at:
        return False
    deadline = _parse_iso_to_epoch_ms(clarification.expires_at)
    if deadline is None:
        return False
    if now is None:
        now = datetime.datetime.now(datetime.timezone.utc).timestamp() * 1000.0
    return deadline <= now
