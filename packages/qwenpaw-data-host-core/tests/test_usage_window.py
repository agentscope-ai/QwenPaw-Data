# -*- coding: utf-8 -*-
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from qwenpaw_data.host.core.domain.usage_window import (
    UsageWindowError,
    background_group_id,
    build_usage_buckets,
    public_model_type,
    resolve_usage_window,
)

AS_OF = datetime(2026, 9, 17, 5, 0, tzinfo=timezone.utc)
NOW = datetime(2026, 9, 17, 6, 0, tzinfo=timezone.utc)


def test_resolve_7d_shanghai_matches_protocol_example() -> None:
    window = resolve_usage_window(
        period="7d",
        timezone_name="Asia/Shanghai",
        as_of=AS_OF,
        now=NOW,
    )
    assert window.start == datetime(2026, 9, 10, 16, 0, tzinfo=timezone.utc)
    assert window.end == AS_OF
    assert window.as_of == AS_OF
    assert window.timezone == "Asia/Shanghai"


def test_resolve_1d_starts_at_local_midnight() -> None:
    window = resolve_usage_window(
        period="1d",
        timezone_name="Asia/Shanghai",
        as_of=AS_OF,
        now=NOW,
    )
    assert window.start == datetime(2026, 9, 16, 16, 0, tzinfo=timezone.utc)


def test_default_timezone_is_shanghai() -> None:
    window = resolve_usage_window(period="7d", timezone_name=None, as_of=AS_OF, now=NOW)
    assert window.timezone == "Asia/Shanghai"


def test_invalid_timezone() -> None:
    with pytest.raises(UsageWindowError) as exc:
        resolve_usage_window(
            period="7d",
            timezone_name="Not/A_Zone",
            as_of=AS_OF,
            now=NOW,
        )
    assert exc.value.reason == "INVALID_TIMEZONE"


def test_as_of_in_future() -> None:
    with pytest.raises(UsageWindowError) as exc:
        resolve_usage_window(
            period="7d",
            timezone_name="Asia/Shanghai",
            as_of=datetime(2026, 9, 18, tzinfo=timezone.utc),
            now=NOW,
        )
    assert exc.value.reason == "AS_OF_IN_FUTURE"


def test_1d_buckets_are_four_hours_and_partial() -> None:
    window = resolve_usage_window(
        period="1d",
        timezone_name="Asia/Shanghai",
        as_of=AS_OF,
        now=NOW,
    )
    buckets = build_usage_buckets(window)
    assert [b.label for b in buckets] == ["00:00", "04:00", "08:00", "12:00"]
    assert buckets[-1].is_partial is True
    assert all(not b.is_partial for b in buckets[:-1])
    assert all(b.granularity == "hour4" for b in buckets)
    assert buckets[0].start == window.start
    assert buckets[-1].start < AS_OF <= buckets[-1].end


def test_7d_buckets_are_calendar_days() -> None:
    window = resolve_usage_window(
        period="7d",
        timezone_name="Asia/Shanghai",
        as_of=AS_OF,
        now=NOW,
    )
    buckets = build_usage_buckets(window)
    assert len(buckets) == 7
    assert buckets[0].label == "09/11"
    assert buckets[-1].label == "09/17"
    assert buckets[-1].is_partial is True
    assert all(b.granularity == "day" for b in buckets)


def test_group_id_is_stable() -> None:
    assert background_group_id("data-agent") == background_group_id("data-agent")
    assert background_group_id("data-agent") != background_group_id("skill-create")
    assert background_group_id("data-agent").startswith("bg_")


def test_public_model_type_maps_chat_to_text() -> None:
    assert public_model_type("chat") == "text"
    assert public_model_type("text") == "text"
    assert public_model_type("embedding") == "embedding"
    assert public_model_type("image") is None
