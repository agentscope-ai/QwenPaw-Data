# -*- coding: utf-8 -*-
from __future__ import annotations

from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class RuntimeSettings(BaseSettings):
    """Chat 运行时行为参数。"""

    model_config = SettingsConfigDict(
        env_prefix="QWENPAW_DATA_RUNTIME_", extra="ignore", validate_by_name=True
    )

    # Transformer.join 等算法侧刷完 segment 的上限。
    transformer_join_timeout_s: float = 90.0

    # 用户偏好默认。
    react_max_iters: int = 10000
    llm_retry_enabled: bool = True
    llm_max_retries: int = 3
    context_trigger_ratio: float = 0.8
    context_reserve_ratio: float = 0.1
    context_max_trigger_ratio: float = Field(default=0.9, gt=0, le=0.9)
    # 上下文占用率 * context_occupancy_to_trigger_ratio = 压缩触发阈值
    context_occupancy_to_trigger_ratio: float = Field(default=0.95, gt=0, le=1)
    tool_result_limit: int = 50000
    tool_timeout_enabled: bool = True
    # 0.3 name QWENPAW_DATA_TOOL_TIMEOUT_SECONDS applies when the new one is unset.
    tool_timeout_seconds: float = Field(
        default=1800,
        ge=1,
        validation_alias=AliasChoices(
            "QWENPAW_DATA_RUNTIME_TOOL_TIMEOUT_SECONDS",
            "QWENPAW_DATA_TOOL_TIMEOUT_SECONDS",
        ),
    )

    # ChatRecovery 扫描 running Chat 的间隔。
    recover_interval_seconds: float = Field(default=10, gt=0)

    inherit_plan: bool = True
