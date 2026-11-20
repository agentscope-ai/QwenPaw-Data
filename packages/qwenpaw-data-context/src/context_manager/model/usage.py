"""Capture model usage without changing model results or request ownership."""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from time import perf_counter
from typing import Any, Literal
from uuid import uuid4

log = logging.getLogger(__name__)
_ModelType = Literal["chat", "embedding"]


@dataclass(frozen=True, slots=True)
class UsageContext:
    """Carry attribution only; model dependencies supply the trusted identity."""

    session_id: str = ""
    chat_id: str = ""
    source: str = "其他"
    source_step: str = "other"
    probe: bool = False


_usage_context: ContextVar[UsageContext] = ContextVar(
    "model_usage_context", default=UsageContext()
)


def current_usage_context() -> UsageContext:
    return _usage_context.get()


@contextmanager
def bind_usage_context(
    context: UsageContext | None = None,
    *,
    session_id: str | None = None,
    chat_id: str | None = None,
    source: str | None = None,
    source_step: str | None = None,
    probe: bool | None = None,
) -> Iterator[UsageContext]:
    """Override a supplied snapshot or the current scope, restoring it on exit."""
    overrides = {
        key: value
        for key, value in (
            ("session_id", session_id),
            ("chat_id", chat_id),
            ("source", source),
            ("source_step", source_step),
            ("probe", probe),
        )
        if value is not None
    }
    bound = replace(
        context if context is not None else current_usage_context(), **overrides
    )
    token = _usage_context.set(bound)
    try:
        yield bound
    finally:
        _usage_context.reset(token)


@dataclass(frozen=True, slots=True)
class ModelUsageEvent:
    """Validate a completed usage record once, before queueing or persistence."""

    id: str
    user_id: str
    tenant_id: str
    workspace_id: str
    session_id: str
    chat_id: str
    source: str
    source_step: str
    model_type: _ModelType
    provider_id: str
    model_id: str
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int
    cache_creation_tokens: int
    duration_ms: int
    created_at: datetime

    def __post_init__(self) -> None:
        required = (
            self.id,
            self.user_id,
            self.tenant_id,
            self.workspace_id,
            self.source,
            self.source_step,
            self.provider_id,
            self.model_id,
        )
        if any(not isinstance(value, str) or not value.strip() for value in required):
            raise ValueError("model usage event has an empty required field")
        if self.model_type not in {"chat", "embedding"}:
            raise ValueError("model usage event has an invalid model_type")
        if not isinstance(self.session_id, str) or not isinstance(self.chat_id, str):
            raise ValueError("model usage session and chat must be strings")
        if not self.session_id and self.chat_id:
            raise ValueError("background model usage cannot carry chat_id")
        counters = (
            self.input_tokens,
            self.output_tokens,
            self.cache_read_tokens,
            self.cache_creation_tokens,
            self.duration_ms,
        )
        if any(not isinstance(value, int) or value < 0 for value in counters):
            raise ValueError("model usage counters must be non-negative integers")
        if self.created_at.tzinfo is None or self.created_at.utcoffset() is None:
            raise ValueError("model usage created_at must be timezone-aware")


@dataclass(frozen=True, slots=True)
class ModelUsageTracker:
    """Bind operation identity and capture each SDK response before validation."""

    enqueue: Callable[[ModelUsageEvent], bool] = field(repr=False)
    tenant_id: str = field(kw_only=True)
    workspace_id: str = field(kw_only=True)
    user_id: str = field(kw_only=True)
    provider_id: str = field(kw_only=True)

    @contextmanager
    def track_call(
        self,
        model_type: _ModelType,
        model_id: str,
    ) -> Iterator[Callable[[object], bool]]:
        context = current_usage_context()
        session_id = context.session_id.strip()
        chat_id = context.chat_id.strip() if session_id else ""
        usage_id = f"usage-{uuid4()}"
        started = perf_counter()

        def record_response(response: object) -> bool:
            if context.probe:
                return False
            try:
                duration_ms = int((perf_counter() - started) * 1000)
                completed_at = datetime.now(timezone.utc)
                counts = self._usage_counts(
                    self._read_field(response, "usage"), model_type
                )
                response_model = self._read_field(response, "model") or model_id or ""
                event = ModelUsageEvent(
                    id=usage_id,
                    user_id=self.user_id,
                    tenant_id=self.tenant_id,
                    workspace_id=self.workspace_id,
                    session_id=session_id,
                    chat_id=chat_id,
                    source="cm" if session_id else context.source,
                    source_step=context.source_step,
                    model_type=model_type,
                    provider_id=self.provider_id,
                    model_id=str(response_model).strip() or "unknown",
                    input_tokens=counts[0],
                    output_tokens=counts[1],
                    cache_read_tokens=counts[2],
                    cache_creation_tokens=counts[3],
                    duration_ms=max(0, duration_ms),
                    created_at=completed_at,
                )
                return self.enqueue(event)
            except Exception as exc:
                log.warning("Model usage recording failed (%s)", type(exc).__name__)
                return False

        # Do not catch exceptions raised by the SDK or business validation.
        yield record_response

    @staticmethod
    def _read_field(obj: Any, *names: str) -> Any:
        for name in names:
            if isinstance(obj, dict) and obj.get(name) is not None:
                return obj[name]
            value = getattr(obj, name, None)
            if value is not None:
                return value
        extra = getattr(obj, "model_extra", None)
        if isinstance(extra, dict):
            for name in names:
                if extra.get(name) is not None:
                    return extra[name]
        return None

    @staticmethod
    def _non_negative_int(value: Any) -> int:
        try:
            return max(0, int(value or 0))
        except (TypeError, ValueError):
            return 0

    @classmethod
    def _usage_counts(
        cls, usage: Any, model_type: _ModelType
    ) -> tuple[int, int, int, int]:
        if usage is None:
            return 0, 0, 0, 0
        if model_type == "embedding":
            return (
                cls._non_negative_int(cls._read_field(usage, "total_tokens")),
                0,
                0,
                0,
            )
        input_tokens = cls._non_negative_int(
            cls._read_field(usage, "prompt_tokens", "input_tokens")
        )
        output_tokens = cls._non_negative_int(
            cls._read_field(usage, "completion_tokens", "output_tokens")
        )
        details = cls._read_field(
            usage, "prompt_tokens_details", "input_tokens_details"
        )
        cache_read = cls._read_field(details, "cached_tokens")
        if cache_read is None:
            cache_read = cls._read_field(
                usage, "cache_read_tokens", "cache_input_tokens"
            )
        cache_creation = cls._read_field(details, "cache_creation_input_tokens")
        if cache_creation is None:
            cache_creation = cls._read_field(usage, "cache_creation_input_tokens")
        return (
            input_tokens,
            output_tokens,
            cls._non_negative_int(cache_read),
            cls._non_negative_int(cache_creation),
        )
