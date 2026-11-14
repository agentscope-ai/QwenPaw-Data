# -*- coding: utf-8 -*-
from __future__ import annotations

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class CronSettings(BaseSettings):
    """定时任务轮询与本机并发上限。"""

    model_config = SettingsConfigDict(env_prefix="QWENPAW_DATA_CRON_", extra="ignore")

    # 隔多少秒去检查数据库，看有没有到点 / 被点了立刻跑的任务。
    poll_seconds: float = Field(default=1, gt=0)
    # 这台机器同时最多跑几场定时任务。绑了同一会话的，仍要等上一场结束。
    max_threads: int = Field(default=8, ge=1)
    # 这次执行超过该秒数没有刷新 heartbeat_at，就清掉占用，不马上再开一轮。
    running_ttl_seconds: float = Field(default=90, gt=0)


__all__ = ["CronSettings"]
