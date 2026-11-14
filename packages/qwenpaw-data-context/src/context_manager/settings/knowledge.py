"""Configure Knowledge upload limits, concurrency and model-call timeout."""

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class KnowledgeSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="KNOWLEDGE_", extra="ignore")

    ingest_llm_timeout: float | None = None
    max_upload_bytes: int = Field(default=50 * 1024 * 1024, ge=1, le=100 * 1024 * 1024)
    max_document_pages: int = Field(default=100, ge=1, le=1000)
    max_document_chars: int = Field(default=500_000, ge=1, le=5_000_000)
    max_concurrency: int = Field(default=4, ge=1, le=32)

    @field_validator("ingest_llm_timeout", mode="before")
    @classmethod
    def _timeout(cls, value: object) -> float | None:
        try:
            parsed = float(value)
        except (TypeError, ValueError):
            return None
        return parsed if parsed > 0 else None
