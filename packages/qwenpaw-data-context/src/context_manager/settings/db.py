"""Configure the shared relational database used by CM."""

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from qwenpaw_data_auth.database import shared_database_url


class DbSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="QWENPAW_DATA_DB_", extra="ignore", hide_input_in_errors=True
    )

    url: str = Field(default="", repr=False, validate_default=True)
    pool_recycle_seconds: int = Field(default=600, gt=0)
    sqlite_busy_timeout_ms: int = Field(default=5000, ge=0)

    @field_validator("url")
    @classmethod
    def _default_to_shared_database(cls, value: str) -> str:
        # Unset selects the control-plane database shared with Auth and Host.
        return value or shared_database_url()
