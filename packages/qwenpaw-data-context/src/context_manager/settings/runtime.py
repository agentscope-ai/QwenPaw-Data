"""Configure the fixed graph route served in local single-user mode."""

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class RuntimeSettings(BaseSettings):
    """Load the Neo4j route bound to the reserved local Workspace."""

    model_config = SettingsConfigDict(
        extra="ignore",
        populate_by_name=True,
        hide_input_in_errors=True,
    )

    neo4j_uri: str = "bolt://localhost:7687"
    neo4j_user: str = "neo4j"
    neo4j_password: str = Field(default="", repr=False)
    neo4j_database: str | None = None
