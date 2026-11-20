# -*- coding: utf-8 -*-
"""Settlement 运行时配置。

模型不在这里配置：manager 从用户 preferences 加载并构建。
"""

from __future__ import annotations

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class SettlementSettings(BaseSettings):
    """运行时配置"""

    model_config = SettingsConfigDict(env_prefix="QWENPAW_DATA_SETTLEMENT_", extra="ignore")

    enabled: bool = True
    window_size: int = 3
    pool_summary_limit: int = 20
    dismissed_summary_limit: int = 20
    confirmer_concurrency: int = 4
    llm_timeout: float = 30.0
    llm_attempts: int = 2
    cm_timeout: float = 180.0
    retrieval_max_iters: int = Field(default=8, ge=1)
