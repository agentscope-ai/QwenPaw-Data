# -*- coding: utf-8 -*-
"""One strict-JSON call on the chat model the host builds for us.

Deliberately thinner than the converter's client: this call sits inside the
end-of-turn budget, so there is no room to retry and no reason to reason. A
failure is a failure, and the rules channel answers instead.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import TYPE_CHECKING, Any

from agentscope.message import UserMsg

if TYPE_CHECKING:
    from agentscope.model import ChatModelBase

logger = logging.getLogger(__name__)

# Only the question and its intent are asked for. Decoding length dominates the
# end-to-end latency here, and the entities are recovered locally from the
# question text at least as well as the model reports them.
QUESTIONS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "questions": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "text": {"type": "string"},
                    "intent": {
                        "type": "string",
                        "enum": [
                            "drilldown",
                            "comparison",
                            "attribution",
                            "adjacent",
                            "synthesis",
                        ],
                    },
                },
                "required": ["text", "intent"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["questions"],
    "additionalProperties": False,
}
# Thinking is off on purpose, not by luck: reasoning tokens earn nothing against
# a fixed schema, and they would spend the whole turn budget on their own.
_CALL_PROFILE: dict[str, Any] = {"thinking_enable": False, "temperature": 0.0}


class FollowUpLLMError(RuntimeError):
    """Raised when the model cannot produce a usable payload."""


def for_structured_calls(model: ChatModelBase) -> ChatModelBase:
    """Retune a model the host built for the agent into one for generation.

    The call asks for one JSON object, so streaming buys nothing. Only knobs
    the provider declares are touched, so an API without them cannot fail the
    turn.
    """

    model.stream = False
    fields = type(model.parameters).model_fields
    update = {key: value for key, value in _CALL_PROFILE.items() if key in fields}
    if update:
        model.parameters = model.parameters.model_copy(update=update)
    return model


class FollowUpLLM:
    """Ask one light model for follow-up candidates as a JSON object."""

    def __init__(self, model: ChatModelBase, *, timeout: float) -> None:
        self.model = model
        self.timeout = timeout

    async def complete(self, prompt: str) -> dict[str, Any]:
        """Return the parsed object, raising FollowUpLLMError on any failure."""
        try:
            response = await asyncio.wait_for(
                self.model.generate_structured_output(
                    messages=[UserMsg(name="user", content=prompt)],
                    structured_model=QUESTIONS_SCHEMA,
                ),
                timeout=self.timeout,
            )
        # The transport is provider SDK code now, and a small model missing the
        # schema is not an exception type worth naming: anything short of
        # cancellation leaves the rules channel to answer.
        except Exception as exc:  # noqa: BLE001
            raise FollowUpLLMError(str(exc) or type(exc).__name__) from exc
        payload = response.content
        if not isinstance(payload, dict):
            raise FollowUpLLMError("response is not a JSON object")
        return _decode_questions(payload)


def _decode_questions(payload: dict[str, Any]) -> dict[str, Any]:
    """Accept the questions array written as its own JSON text.

    A small model routinely fills a nested array into the tool call as a
    string. It is the same value either way, and rejecting it would cost the
    whole batch on a call that has no second attempt.
    """

    questions = payload.get("questions")
    if not isinstance(questions, str):
        return payload
    try:
        parsed = json.loads(questions)
    except ValueError:
        return payload
    if not isinstance(parsed, list):
        return payload
    logger.info("decoded questions from a JSON string into a list")
    return {**payload, "questions": parsed}


__all__ = [
    "QUESTIONS_SCHEMA",
    "FollowUpLLM",
    "FollowUpLLMError",
    "for_structured_calls",
]
