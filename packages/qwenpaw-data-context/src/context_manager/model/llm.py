"""Provide asynchronous text generation with a configured SDK client."""

from __future__ import annotations

from collections.abc import Sequence
from contextlib import nullcontext
from dataclasses import dataclass
from types import TracebackType
from typing import Any, Literal, Protocol, Self, runtime_checkable

import httpx
from openai import AsyncOpenAI

from ..settings import get_settings
from ..utils.validation import (
    optional_trimmed_text,
    require_nonempty_text,
    require_trimmed_text,
)
from .usage import ModelUsageTracker

ChatRole = Literal["system", "developer", "user", "assistant"]


class LlmError(RuntimeError):
    """Report a provider failure or an unusable provider response."""


@dataclass(frozen=True, slots=True)
class ChatMessage:
    """Represent one text-only message accepted by the generic caller."""

    role: ChatRole
    content: str

    def __post_init__(self) -> None:
        """Reject malformed roles and empty message content before transport."""
        if self.role not in {"system", "developer", "user", "assistant"}:
            raise ValueError(f"unsupported chat role: {self.role}")
        require_nonempty_text(self.content, "message content")


@runtime_checkable
class LlmCaller(Protocol):
    """Define the asynchronous text-generation boundary."""

    async def complete(
        self,
        messages: Sequence[ChatMessage],
        *,
        temperature: float = 0.2,
        max_tokens: int | None = None,
        model: str | None = None,
        json_mode: bool = False,
        enable_reasoning: bool | None = None,
        timeout_seconds: float | None = None,
    ) -> str:
        """Complete text; an optional timeout overrides the client for this call."""
        ...

    async def aclose(self) -> None:
        """Release transport resources owned by the caller."""
        ...


class OpenAILlmCaller:
    """Call text-completion endpoints using constructor-supplied model defaults."""

    def __init__(
        self,
        *,
        api_key: str | None = None,
        base_url: str | None = None,
        model: str | None = None,
        timeout_seconds: float | None = None,
        max_retries: int | None = None,
        client: Any | None = None,
        usage_tracker: ModelUsageTracker | None = None,
    ) -> None:
        """Capture model defaults and optionally borrow an existing SDK client."""
        self._usage_tracker = usage_tracker
        self._model = optional_trimmed_text(model, "model")
        self._base_url = optional_trimmed_text(base_url, "base_url")
        if max_retries is None:
            max_retries = get_settings().model.sdk_max_retries
        if max_retries < 0:
            raise ValueError("max_retries must be non-negative")
        self._closed = False
        self._owns_client = client is None
        if client is not None:
            self._client = client
            return
        try:
            api_key = require_nonempty_text(api_key, "api_key")
        except (TypeError, ValueError) as exc:
            raise ValueError("api_key is required when client is not provided") from exc
        options: dict[str, object] = {
            "api_key": api_key,
            "max_retries": max_retries,
        }
        timeout = (
            get_settings().model.timeout if timeout_seconds is None else timeout_seconds
        )
        if timeout is not None and timeout > 0:
            options["timeout"] = timeout
        if self._base_url is not None:
            options["base_url"] = self._base_url
        if get_settings().model.ignore_proxy:
            options["http_client"] = httpx.AsyncClient(trust_env=False)
        self._client = AsyncOpenAI(**options)

    def _require_open(self) -> None:
        if self._closed:
            raise LlmError("model caller is closed")

    @staticmethod
    def _resolve_model(
        override: str | None,
        configured: str | None,
        purpose: str,
    ) -> str:
        """Resolve and validate one per-call or caller-level model name."""
        resolved = override if override is not None else configured
        try:
            return require_trimmed_text(resolved, f"{purpose} model")
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{purpose} model must be configured and trimmed") from exc

    async def complete(
        self,
        messages: Sequence[ChatMessage],
        *,
        temperature: float = 0.2,
        max_tokens: int | None = None,
        model: str | None = None,
        json_mode: bool = False,
        enable_reasoning: bool | None = None,
        timeout_seconds: float | None = None,
    ) -> str:
        """Return a non-empty text answer from an OpenAI-compatible endpoint."""
        self._require_open()
        if not isinstance(messages, Sequence) or isinstance(messages, (str, bytes)):
            raise TypeError("messages must be a sequence of ChatMessage")
        if not messages:
            raise ValueError("messages must not be empty")
        if any(type(message) is not ChatMessage for message in messages):
            raise TypeError("messages must contain only ChatMessage values")
        if not 0 <= temperature <= 2:
            raise ValueError("temperature must be between 0 and 2")
        if max_tokens is not None and max_tokens <= 0:
            raise ValueError("max_tokens must be positive")
        if type(json_mode) is not bool:
            raise TypeError("json_mode must be a boolean")
        if enable_reasoning is not None and type(enable_reasoning) is not bool:
            raise TypeError("enable_reasoning must be a boolean or None")
        if timeout_seconds is not None and timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")

        options: dict[str, object] = {
            "model": self._resolve_model(model, self._model, "chat"),
            "messages": [
                {"role": message.role, "content": message.content}
                for message in messages
            ],
            "temperature": temperature,
        }
        if max_tokens is not None:
            options["max_tokens"] = max_tokens
        if json_mode:
            options["response_format"] = {"type": "json_object"}
        if enable_reasoning is not None:
            # OpenAI-compatible reasoning providers expose this through extra_body.
            options["extra_body"] = {"enable_thinking": enable_reasoning}
        if timeout_seconds is not None:
            options["timeout"] = timeout_seconds
        tracking = (
            self._usage_tracker.track_call(model_type="chat", model_id=options["model"])
            if self._usage_tracker is not None
            else nullcontext()
        )
        with tracking as record_response:
            try:
                response = await self._client.chat.completions.create(**options)
            except Exception:  # noqa: BLE001 - provider exceptions are untrusted.
                raise LlmError("LLM completion request failed") from None
            if record_response is not None:
                record_response(response)

        choices = getattr(response, "choices", None)
        content = (
            getattr(getattr(choices[0], "message", None), "content", None)
            if choices
            else None
        )
        try:
            return require_nonempty_text(content, "LLM completion")
        except (TypeError, ValueError) as exc:
            raise LlmError("LLM completion returned empty text") from exc

    async def aclose(self) -> None:
        """Close only an SDK client created by this caller."""
        if self._closed:
            return
        self._closed = True
        if self._owns_client:
            await self._client.close()

    async def __aenter__(self) -> Self:
        """Return this caller for one application-managed async lifetime."""
        return self

    async def __aexit__(
        self,
        _exc_type: type[BaseException] | None,
        _exc: BaseException | None,
        _traceback: TracebackType | None,
    ) -> None:
        """Close resources when leaving the async context manager."""
        await self.aclose()


__all__ = ["ChatMessage", "LlmCaller", "LlmError", "OpenAILlmCaller"]
