"""Configure User Graph extraction budgets."""

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class UserSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="CM_USER_", extra="ignore")

    max_concurrency: int = Field(default=4, ge=1, le=32)
