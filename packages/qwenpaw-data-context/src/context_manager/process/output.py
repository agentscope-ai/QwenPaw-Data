"""Request JSON objects with optional schema validation and bounded retries."""

from __future__ import annotations

import json
from collections.abc import Sequence

from jsonschema import ValidationError, validate

from ..model import ChatMessage, LlmCaller
from ..utils.parsing import parse_json_object


async def complete_json(
    caller: LlmCaller,
    messages: Sequence[ChatMessage],
    *,
    schema: dict | None = None,
    attempts: int = 1,
    max_tokens: int | None = None,
    timeout_seconds: float | None = None,
) -> dict[str, object]:
    """Validate model output and retry JSON/schema failures within the budget."""
    if attempts < 1:
        raise ValueError("attempts must be positive")
    request = list(messages)
    if schema is not None:
        instruction = (
            "The response must match this JSON Schema exactly:\n"
            + json.dumps(schema, ensure_ascii=False)
        )
        system = next(
            (i for i, message in enumerate(request) if message.role == "system"), None
        )
        if system is None:
            request.insert(0, ChatMessage("system", instruction))
        else:
            request[system] = ChatMessage(
                "system", request[system].content + "\n\n" + instruction
            )
    timeout_options = (
        {} if timeout_seconds is None else {"timeout_seconds": timeout_seconds}
    )
    for attempt in range(attempts):
        response = await caller.complete(
            tuple(request),
            temperature=0,
            max_tokens=max_tokens,
            json_mode=True,
            **timeout_options,
        )
        try:
            parsed = parse_json_object(response, "model response")
            if schema is not None:
                validate(parsed, schema)
            return parsed
        except (TypeError, ValueError, ValidationError) as exc:
            if attempt + 1 == attempts:
                raise ValueError(
                    f"Model JSON response failed after {attempts} attempts"
                ) from exc


__all__ = ["complete_json"]
