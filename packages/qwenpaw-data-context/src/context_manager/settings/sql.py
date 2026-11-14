"""Configure SQL result artifact retention."""

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class SqlSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="CM_SQL_", extra="ignore")

    retention_seconds: int = Field(default=24 * 60 * 60, gt=0)
