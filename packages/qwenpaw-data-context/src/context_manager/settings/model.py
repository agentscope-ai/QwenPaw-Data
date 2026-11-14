from pydantic import AliasChoices, Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from qwenpaw_data_auth.config import local_master_secret

from .paths import PathsSettings


class ModelSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="LLM_HTTP_",
        extra="ignore",
        hide_input_in_errors=True,
        populate_by_name=True,
    )

    timeout: float | None = 300.0
    ignore_proxy: bool = False
    sdk_max_retries: int = Field(default=2, ge=0)
    env: str = Field(default="development", validation_alias="QWENPAW_DATA_ENV")
    preferences_master_secret: str = Field(
        default="",
        repr=False,
        validation_alias=AliasChoices(
            "QWENPAW_DATA_PREFERENCES_MASTER_SECRET", "QWENPAW_DATA_PREFS_MASTER_SECRET"
        ),
    )

    def master_key(self) -> bytes:
        secret = self.preferences_master_secret.strip()
        if not secret:
            if self.env.strip().lower() in {"prod", "production"}:
                raise RuntimeError("QWENPAW_DATA_PREFERENCES_MASTER_SECRET must be set in production")
            secret = local_master_secret(PathsSettings().home)
        try:
            key = bytes.fromhex(secret)
            if len(secret) != 64 or len(key) != 32:
                raise ValueError
        except ValueError:
            raise RuntimeError("QWENPAW_DATA_PREFERENCES_MASTER_SECRET must be 64 hex characters") from None
        return key

    @field_validator("ignore_proxy", mode="before")
    @classmethod
    def _proxy(cls, value: object) -> bool:
        return str(value).strip().lower() not in {"", "0", "false", "no", "off"}

    @field_validator("timeout", mode="before")
    @classmethod
    def _timeout(cls, value: object) -> float | None:
        if value is None:
            return None
        try:
            parsed = float(value)
        except (TypeError, ValueError):
            return 300.0
        return None if parsed <= 0 else parsed
