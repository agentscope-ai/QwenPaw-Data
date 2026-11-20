"""Configure embedding batches, retries and the process-local cache."""

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class EmbeddingSettings(BaseSettings):
    model_config = SettingsConfigDict(extra="ignore", populate_by_name=True)

    batch_size: int = Field(default=10, validation_alias="EMBED_API_BATCH")
    max_concurrency: int = Field(default=24, validation_alias="EMBED_API_CONCURRENCY")
    rate_limit_retries: int = Field(default=5, validation_alias="EMBED_API_MAX_RETRIES")
    sdk_max_retries: int = Field(
        default=1, ge=0, validation_alias="EMBED_SDK_MAX_RETRIES"
    )
    max_input_chars: int = Field(
        default=8000, validation_alias="EMBED_API_MAX_INPUT_CHARS"
    )
    cache_ttl_seconds: float = Field(
        default=600.0, validation_alias="CM_EMBED_CACHE_TTL_SEC"
    )
    cache_max_entries: int = Field(default=2048, validation_alias="EMBED_CACHE_MAX")
    dimensions: int | None = Field(default=None, validation_alias="EMBED_DIM")

    @field_validator("batch_size", "max_concurrency", "cache_max_entries")
    @classmethod
    def _positive(cls, value: int) -> int:
        return max(1, value)

    @field_validator("rate_limit_retries")
    @classmethod
    def _retries(cls, value: int) -> int:
        return max(0, value)

    @field_validator("max_input_chars")
    @classmethod
    def _input_chars(cls, value: int) -> int:
        return max(100, value)

    @field_validator("dimensions", mode="before")
    @classmethod
    def _dimensions(cls, value: object) -> int | None:
        if value is None or value == "":
            return None
        try:
            parsed = int(value)
        except (TypeError, ValueError) as exc:
            raise ValueError("EMBED_DIM must be a positive integer") from exc
        if parsed <= 0:
            raise ValueError("EMBED_DIM must be a positive integer")
        return parsed
