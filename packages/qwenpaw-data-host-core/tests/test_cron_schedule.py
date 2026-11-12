# -*- coding: utf-8 -*-
from __future__ import annotations

from datetime import timedelta

from qwenpaw_data.host.core.api.models.cron import ScheduleSpec
from qwenpaw_data.host.core.utils.cron import compute_next_run_at
from qwenpaw_data.host.core.utils.time import utcnow


def test_once_keeps_future_run_at() -> None:
    run_at = utcnow() + timedelta(minutes=5)
    schedule = ScheduleSpec(type="once", run_at=run_at, timezone="UTC")
    assert compute_next_run_at(schedule) == schedule.run_at


def test_once_clears_when_run_at_has_arrived() -> None:
    run_at = utcnow() - timedelta(seconds=1)
    schedule = ScheduleSpec(type="once", run_at=run_at, timezone="UTC")
    assert compute_next_run_at(schedule) is None
