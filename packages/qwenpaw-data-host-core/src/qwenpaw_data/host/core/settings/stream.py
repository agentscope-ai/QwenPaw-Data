# -*- coding: utf-8 -*-
from __future__ import annotations

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class StreamSettings(BaseSettings):
    """Chat SSE 传输参数。"""

    model_config = SettingsConfigDict(env_prefix="QWENPAW_DATA_STREAM_", extra="ignore")

    # 空闲超过该秒数发 `: keepalive` 注释，避免网关掐掉无事件的流。
    sse_heartbeat_seconds: float = Field(default=15, gt=0)

    # GET /events 读 chat_events 的间隔。
    sse_poll_seconds: float = Field(default=0.05, gt=0)

    # 攒够该条数的 content delta 才批量落盘并 commit。
    delta_flush_size: int = Field(default=32, gt=0)

    # running Chat 超过该秒数没有心跳，停止接口直接收尾。
    heartbeat_ttl_seconds: float = Field(default=30, gt=0)

    # running Chat 写心跳的间隔。
    heartbeat_interval_seconds: float = Field(default=10, gt=0)


__all__ = ["StreamSettings"]
