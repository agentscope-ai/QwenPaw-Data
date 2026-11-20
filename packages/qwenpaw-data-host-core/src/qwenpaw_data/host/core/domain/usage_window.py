# -*- coding: utf-8 -*-
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone
from typing import Any, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

UsagePeriod = Literal["1d", "7d", "30d"]
UsageGranularity = Literal["hour4", "day"]

DEFAULT_TIMEZONE = "Asia/Shanghai"
_PERIOD_DAYS = {"1d": 1, "7d": 7, "30d": 30}
_PUBLIC_MODEL_TYPE = {"chat": "text", "text": "text", "embedding": "embedding"}


class UsageWindowError(ValueError):
    def __init__(self, message: str, *, reason: str, field: str) -> None:
        super().__init__(message)
        self.reason = reason
        self.field = field


@dataclass(frozen=True)
class UsageWindow:
    period: UsagePeriod
    start: datetime
    end: datetime
    timezone: str
    as_of: datetime


@dataclass(frozen=True)
class UsageBucket:
    start: datetime
    end: datetime
    label: str
    is_partial: bool
    granularity: UsageGranularity


@dataclass(frozen=True)
class GenerationUsage:
    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
    calls: int = 0

    @classmethod
    def from_parts(
        cls, input_tokens: Any, output_tokens: Any, calls: Any
    ) -> GenerationUsage:
        inp, out, n = int(input_tokens or 0), int(output_tokens or 0), int(calls or 0)
        return cls(inp, out, inp + out, n)

    def add(self, other: GenerationUsage) -> GenerationUsage:
        return GenerationUsage.from_parts(
            self.input_tokens + other.input_tokens,
            self.output_tokens + other.output_tokens,
            self.calls + other.calls,
        )


@dataclass(frozen=True)
class EmbeddingUsage:
    tokens: int = 0
    calls: int = 0

    @classmethod
    def from_parts(cls, tokens: Any, calls: Any) -> EmbeddingUsage:
        return cls(int(tokens or 0), int(calls or 0))

    def add(self, other: EmbeddingUsage) -> EmbeddingUsage:
        return EmbeddingUsage.from_parts(
            self.tokens + other.tokens, self.calls + other.calls
        )


def _ensure_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def resolve_usage_window(
    *,
    period: str,
    timezone_name: str | None,
    as_of: datetime | None,
    now: datetime,
) -> UsageWindow:
    if period not in _PERIOD_DAYS:
        raise UsageWindowError(
            "period must be 1d, 7d, or 30d",
            reason="INVALID_PERIOD",
            field="period",
        )
    tz_name = (timezone_name or "").strip() or DEFAULT_TIMEZONE
    try:
        tz = ZoneInfo(tz_name)
    except (ZoneInfoNotFoundError, KeyError) as exc:
        raise UsageWindowError(
            "timezone is not a valid IANA timezone",
            reason="INVALID_TIMEZONE",
            field="timezone",
        ) from exc
    now_utc = _ensure_utc(now)
    as_of_utc = now_utc if as_of is None else _ensure_utc(as_of)
    if as_of_utc > now_utc:
        raise UsageWindowError(
            "as_of must not be in the future",
            reason="AS_OF_IN_FUTURE",
            field="as_of",
        )
    local_as_of = as_of_utc.astimezone(tz)
    local_start = datetime.combine(
        local_as_of.date() - timedelta(days=_PERIOD_DAYS[period] - 1),
        time.min,
        tzinfo=tz,
    )
    return UsageWindow(
        period=period,  # type: ignore[arg-type]
        start=local_start.astimezone(timezone.utc),
        end=as_of_utc,
        timezone=tz_name,
        as_of=as_of_utc,
    )


def build_usage_buckets(window: UsageWindow) -> list[UsageBucket]:
    tz = ZoneInfo(window.timezone)
    local_cursor = window.start.astimezone(tz)
    granularity: UsageGranularity = "hour4" if window.period == "1d" else "day"
    label_fmt = "%H:%M" if granularity == "hour4" else "%m/%d"
    buckets: list[UsageBucket] = []
    while local_cursor.astimezone(timezone.utc) < window.as_of:
        if granularity == "hour4":
            local_end = local_cursor + timedelta(hours=4)
        else:
            local_end = datetime.combine(
                local_cursor.date() + timedelta(days=1), time.min, tzinfo=tz
            )
        start = local_cursor.astimezone(timezone.utc)
        end = local_end.astimezone(timezone.utc)
        buckets.append(
            UsageBucket(
                start=start,
                end=end,
                label=local_cursor.strftime(label_fmt),
                is_partial=start <= window.as_of < end,
                granularity=granularity,
            )
        )
        local_cursor = local_end
    return buckets


def background_group_id(source: str) -> str:
    return f"bg_{hashlib.sha256(source.encode('utf-8')).hexdigest()[:12]}"


def public_model_type(raw: str) -> str | None:
    return _PUBLIC_MODEL_TYPE.get(raw)
