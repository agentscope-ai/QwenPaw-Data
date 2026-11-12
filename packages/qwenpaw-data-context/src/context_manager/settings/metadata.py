"""Configure physical metadata discovery budgets."""

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class MetadataSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="CM_METADATA_", extra="ignore")

    cache_ttl_seconds: float = Field(default=600, ge=0)
    sample_timeout_ms: int = Field(default=3000, ge=100, le=30_000)
