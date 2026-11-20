# -*- coding: utf-8 -*-
from __future__ import annotations

import asyncio
import json
import logging
from typing import TYPE_CHECKING, Any

from agentscope.message import UserMsg

if TYPE_CHECKING:
    from agentscope.model import ChatModelBase

logger = logging.getLogger(__name__)

PATCHES_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "summary": {"type": "string"},
        "patches": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "skill_name": {"type": "string"},
                    "relative_path": {"type": "string"},
                    "old_string": {"type": ["string", "null"]},
                    "new_string": {"type": "string"},
                    "create_file": {"type": "boolean"},
                    "reason": {"type": "string"},
                },
                "required": ["skill_name", "relative_path", "new_string", "reason"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["summary", "patches"],
    "additionalProperties": False,
}

_CALL_PROFILE: dict[str, Any] = {"thinking_enable": False, "temperature": 0.0}


class SkillEvolutionLLMError(RuntimeError):
    """Raised when the model cannot produce a usable evolution payload."""


def for_structured_calls(model: ChatModelBase) -> ChatModelBase:
    model.stream = False
    fields = type(model.parameters).model_fields
    update = {key: value for key, value in _CALL_PROFILE.items() if key in fields}
    if update:
        model.parameters = model.parameters.model_copy(update=update)
    return model


class SkillEvolutionLLM:
    def __init__(self, model: ChatModelBase, *, timeout: float) -> None:
        self.model = model
        self.timeout = timeout

    async def complete(self, prompt: str) -> dict[str, Any]:
        try:
            response = await asyncio.wait_for(
                self.model.generate_structured_output(
                    messages=[UserMsg(name="user", content=prompt)],
                    structured_model=PATCHES_SCHEMA,
                ),
                timeout=self.timeout,
            )
        except Exception as exc:  # noqa: BLE001
            raise SkillEvolutionLLMError(str(exc) or type(exc).__name__) from exc
        payload = response.content
        if not isinstance(payload, dict):
            raise SkillEvolutionLLMError("response is not a JSON object")
        return _decode_patches(payload)


def _decode_patches(payload: dict[str, Any]) -> dict[str, Any]:
    patches = payload.get("patches")
    if isinstance(patches, str):
        try:
            parsed = json.loads(patches)
        except ValueError:
            return payload
        if isinstance(parsed, list):
            return {**payload, "patches": parsed}
    return payload


__all__ = [
    "PATCHES_SCHEMA",
    "SkillEvolutionLLM",
    "SkillEvolutionLLMError",
    "for_structured_calls",
]
