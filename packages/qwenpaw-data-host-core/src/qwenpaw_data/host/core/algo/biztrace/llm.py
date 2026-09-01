# -*- coding: utf-8 -*-
"""Structured JSON-schema calls on the chat model the host builds for us."""

from __future__ import annotations

import asyncio
import json
import logging
import random
from typing import TYPE_CHECKING, Any

from agentscope.message import SystemMsg, UserMsg

if TYPE_CHECKING:
    from agentscope.model import ChatModelBase

logger = logging.getLogger(__name__)

DEFAULT_ATTEMPTS = 2
_BACKOFF_BASE_SECONDS = 0.4
_CONTAINER_TYPES = frozenset({"array", "object"})
_ERROR_CHAR_CAP = 600
_REPAIR_HINT = (
    "Your previous tool call did not match the schema and was rejected:\n\n"
    "{error}\n\n"
    "Answer again, as one call to the same tool. Emit every nested array and "
    "object as real JSON, never as a string, and keep every other field the "
    "type the schema declares."
)
# Thinking is off on purpose, not by luck: reasoning tokens earn nothing against
# a strict schema, DashScope rejects a non-streaming call while thinking is on,
# and it also costs the forced tool call that carries the structured answer.
_CALL_PROFILE: dict[str, Any] = {"thinking_enable": False, "temperature": 0.0}


class StructuredLLMError(RuntimeError):
    """Raised when the model cannot produce a schema-conforming payload."""


def for_structured_calls(model: ChatModelBase) -> ChatModelBase:
    """Retune a model the host built for the agent into one for extraction.

    Every call here asks for one JSON object, so streaming buys nothing and the
    non-streaming path is both cheaper and the one that keeps the schema tool
    call intact. Only knobs the provider declares are touched, so an API
    without them cannot fail the turn.
    """

    model.stream = False
    fields = type(model.parameters).model_fields
    update = {key: value for key, value in _CALL_PROFILE.items() if key in fields}
    if update:
        model.parameters = model.parameters.model_copy(update=update)
    return model


def _declared_types(spec: Any) -> set[str]:
    """Return the JSON types a property schema declares."""
    if not isinstance(spec, dict):
        return set()
    raw = spec.get("type")
    if isinstance(raw, str):
        return {raw}
    if isinstance(raw, list):
        return {item for item in raw if isinstance(item, str)}
    return set()


def _tolerant_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """Let a container property also arrive as the JSON text of that container.

    A small model routinely fills a nested array into the tool call as a
    string, which the transport would otherwise reject before we ever see the
    payload. Only the accepted types widen, and only in the copy handed to the
    provider; callers still validate against the strict schema they passed.
    """

    properties = schema.get("properties")
    if not isinstance(properties, dict):
        return schema
    widened = {
        name: {**spec, "type": sorted(types | {"string"})}
        if (types := _declared_types(spec)) & _CONTAINER_TYPES
        else spec
        for name, spec in properties.items()
    }
    if widened == properties:
        return schema
    return {**schema, "properties": widened}


def _decode_containers(
    payload: dict[str, Any], schema: dict[str, Any]
) -> dict[str, Any]:
    """Undo the encoding ``_tolerant_schema`` allows, losslessly."""
    properties = schema.get("properties")
    if not isinstance(properties, dict):
        return payload
    decoded = dict(payload)
    for name, spec in properties.items():
        value = decoded.get(name)
        types = _declared_types(spec)
        if not isinstance(value, str) or not types & _CONTAINER_TYPES:
            continue
        try:
            parsed = json.loads(value)
        except ValueError:
            continue
        if isinstance(parsed, list | dict):
            logger.info("decoded %s from a JSON string into a container", name)
            decoded[name] = parsed
    return decoded


class StructuredLLM:
    """Ask one chat model for a schema-conforming object, within a budget."""

    def __init__(
        self,
        model: ChatModelBase,
        *,
        timeout: float,
        attempts: int = DEFAULT_ATTEMPTS,
        concurrency: int = 8,
    ) -> None:
        self.model = model
        self.timeout = timeout
        self.attempts = max(1, attempts)
        self._semaphore = asyncio.Semaphore(max(1, concurrency))
        self.failures = 0

    async def complete(
        self,
        *,
        system: str,
        user: str,
        schema: dict[str, Any],
        schema_name: str,
    ) -> dict[str, Any]:
        """Return the parsed object, raising StructuredLLMError on failure."""

        messages = [
            SystemMsg(name="system", content=system),
            UserMsg(name="user", content=user),
        ]
        async with self._semaphore:
            last_error: Exception | None = None
            for attempt in range(self.attempts):
                try:
                    return await self._call(messages, schema, schema_name)
                # The transport is provider SDK code now, and a small model
                # missing the schema is not an exception type worth naming:
                # anything short of cancellation is one failed attempt.
                except Exception as exc:  # noqa: BLE001
                    last_error = exc
                if attempt + 1 < self.attempts:
                    # Temperature is zero, so asking the same question again
                    # earns the same malformed answer; the model has to be told
                    # what was rejected.
                    messages = [
                        *messages,
                        UserMsg(
                            name="user",
                            content=_REPAIR_HINT.format(
                                error=str(last_error)[:_ERROR_CHAR_CAP]
                            ),
                        ),
                    ]
                    await asyncio.sleep(
                        _BACKOFF_BASE_SECONDS * (2**attempt) + random.random() * 0.1
                    )
            self.failures += 1
            raise StructuredLLMError(
                f"{schema_name} generation failed: {last_error}"
            ) from last_error

    async def _call(
        self,
        messages: list[Any],
        schema: dict[str, Any],
        schema_name: str,
    ) -> dict[str, Any]:
        """One attempt, capped by this step's own budget.

        The model retries the calls its provider considers retryable, so the
        cap is what keeps a stalled step from outliving the turn it belongs to.
        """

        response = await asyncio.wait_for(
            self.model.generate_structured_output(
                messages=messages,
                structured_model=_tolerant_schema(schema),
            ),
            timeout=self.timeout,
        )
        payload = response.content
        if not isinstance(payload, dict):
            raise StructuredLLMError(f"{schema_name} response is not a JSON object")
        return _decode_containers(payload, schema)


__all__ = [
    "StructuredLLM",
    "StructuredLLMError",
    "for_structured_calls",
]
