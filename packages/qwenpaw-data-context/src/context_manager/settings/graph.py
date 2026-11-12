"""Configure graph backends independently of application runtime resources."""

from typing import Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class GraphSettings(BaseSettings):
    """Configure the graph backend and native HoloAge connection."""

    model_config = SettingsConfigDict(
        env_prefix="CM_GRAPH_",
        extra="ignore",
        populate_by_name=True,
        hide_input_in_errors=True,
    )

    write_batch_max_rows: int = Field(default=1000, ge=1, le=1000)

    backend: Literal["neo4j", "holoage"] = Field(
        default="neo4j", validation_alias="GRAPH_BACKEND"
    )
    host: str = Field(default="localhost", validation_alias="HOLO_AGE_HOST")
    port: int = Field(default=5432, gt=0, le=65535, validation_alias="HOLO_AGE_PORT")
    user: str = Field(default="postgres", validation_alias="HOLO_AGE_USER")
    password: str = Field(default="", repr=False, validation_alias="HOLO_AGE_PASSWORD")
    database: str = Field(default="postgres", validation_alias="HOLO_AGE_DATABASE")
    graph_name: str = Field(default="cm_graph", validation_alias="HOLO_AGE_GRAPH_NAME")
    holo_pool_max_size: int = Field(default=8, ge=1)
    holo_pool_timeout: float = Field(default=30, gt=0, allow_inf_nan=False)
    holo_pool_max_idle: float = Field(default=300, gt=0, allow_inf_nan=False)

    @field_validator("backend", mode="before")
    @classmethod
    def _backend(cls, value: object) -> object:
        if isinstance(value, str):
            normalized = value.strip().lower()
            return {"holo_age": "holoage", "holo-age": "holoage"}.get(
                normalized, normalized
            )
        return value

    def connection_params(self) -> dict[str, str | int]:
        """Return the driver's connection parameters without opening a socket."""
        return {
            "host": self.host,
            "port": self.port,
            "user": self.user,
            "password": self.password,
            "dbname": self.database,
        }
