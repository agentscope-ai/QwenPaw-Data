"""Resolve the root directory for persistent user files."""

from pathlib import Path

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class PathsSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="QWENPAW_DATA_", extra="ignore")

    home: Path = Field(default_factory=lambda: Path.home() / ".qwenpaw-data")

    @field_validator("home", mode="before")
    @classmethod
    def _home(cls, value: object) -> Path:
        raw = str(value) if value else str(Path.home() / ".qwenpaw-data")
        if raw != raw.strip():
            raise RuntimeError("QWENPAW_DATA_HOME must be trimmed when configured")
        path = Path(raw).expanduser()
        if not path.is_absolute():
            raise ValueError("QWENPAW_DATA_HOME must be absolute")
        return path.resolve()
