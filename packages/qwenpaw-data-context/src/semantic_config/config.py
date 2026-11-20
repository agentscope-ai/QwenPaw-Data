from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from qwenpaw_data.context.env import qwenpaw_data_env_file


class Settings(BaseSettings):
    """应用配置，来源仓库根 .env（dotenv），未知键忽略。"""

    model_config = SettingsConfigDict(
        env_file=str(qwenpaw_data_env_file()), env_file_encoding="utf-8", extra="ignore"
    )

    # Required shared SQL connection: remote Holo or local SQLite, supplied by deployment.
    db_url: str = Field(default="", validation_alias="QWENPAW_DATA_DB_URL")

    tz_offset: str = "+08:00"

    cors_origins: str = "http://localhost:3000,http://127.0.0.1:3000"

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()
