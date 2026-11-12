# -*- coding: utf-8 -*-
from __future__ import annotations

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class ToolConfirmationSettings(BaseSettings):
    """工具确认的 Host 缺省。用户只能改档位。"""

    model_config = SettingsConfigDict(
        env_prefix="QWENPAW_DATA_TOOL_CONFIRMATION_", extra="ignore"
    )

    default_level: str = Field(
        default="auto",
        description="用户未配置时的确认档位：off / auto / all。",
    )
    auto_ask_names: list[str] = Field(
        default_factory=lambda: [],
        description="档位为 auto 时，按调用名原样匹配、需要确认的工具名。",
    )
    ttl_seconds: int = Field(
        default=1800,
        gt=0,
        description="工具确认等待超时时间",
    )
