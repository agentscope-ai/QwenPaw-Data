# -*- coding: utf-8 -*-
from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class OssSettings(BaseSettings):
    """Private OSS bucket used to issue external download links."""

    model_config = SettingsConfigDict(env_prefix="QWENPAW_DATA_OSS_", extra="ignore")

    access_key_id: str = ""
    access_key_secret: str = ""
    endpoint: str = "https://oss-cn-hangzhou.aliyuncs.com"
    bucket: str = ""
    prefix: str = "artifact-shares"
    share_ttl_seconds: int = 604800  # 7 days
    max_bytes: int = 52_428_800  # 50MB

    def require_configured(self) -> None:
        if not (
            self.access_key_id.strip()
            and self.access_key_secret.strip()
            and self.bucket.strip()
        ):
            raise ValueError("OSS is not configured")
