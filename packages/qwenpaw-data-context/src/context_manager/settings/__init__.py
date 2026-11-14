"""Aggregate deployment settings and expose one process-level snapshot."""

from __future__ import annotations

import os
from collections.abc import Mapping
from functools import cached_property, lru_cache
from typing import TYPE_CHECKING

from .db import DbSettings
from .embedding import EmbeddingSettings
from .heartbeat import HeartbeatSettings
from .execution import ExecutionSettings
from .graph import GraphSettings
from .knowledge import KnowledgeSettings
from .metadata import MetadataSettings
from .model import ModelSettings
from .paths import PathsSettings
from .sql import SqlSettings
from .user import UserSettings

if TYPE_CHECKING:
    from qwenpaw_data_auth.config import AuthConfig

    from .cron import CronSettings
    from .runtime import RuntimeSettings


class Settings:
    """Group deployment configuration by responsibility."""

    def __init__(
        self,
        *,
        db: DbSettings | None = None,
        paths: PathsSettings | None = None,
        model: ModelSettings | None = None,
        embedding: EmbeddingSettings | None = None,
        execution: ExecutionSettings | None = None,
        heartbeat: HeartbeatSettings | None = None,
        graph: GraphSettings | None = None,
        knowledge: KnowledgeSettings | None = None,
        metadata: MetadataSettings | None = None,
        sql: SqlSettings | None = None,
        user: UserSettings | None = None,
        environment: Mapping[str, str] | None = None,
    ) -> None:
        self._environment = os.environ if environment is None else environment
        self.db = db or DbSettings()
        self.paths = paths or PathsSettings()
        self.model = model or ModelSettings()
        self.embedding = embedding or EmbeddingSettings()
        self.execution = execution or ExecutionSettings()
        self.heartbeat = heartbeat or HeartbeatSettings()
        self.graph = graph or GraphSettings()
        self.knowledge = knowledge or KnowledgeSettings()
        self.metadata = metadata or MetadataSettings()
        self.sql = sql or SqlSettings()
        self.user = user or UserSettings()
        if self.knowledge.ingest_llm_timeout is None:
            base = self.model.timeout
            self.knowledge.ingest_llm_timeout = (
                900.0 if base is None else max(600.0, base)
            )

    @classmethod
    def load(cls) -> Settings:
        """Load the deployment environment and refresh the settings snapshot."""
        from qwenpaw_data.context.env import load_qwenpaw_data_env

        load_qwenpaw_data_env()
        clear_settings()
        return get_settings()

    def environment_value(self, name: str) -> str | None:
        """Read a variable named by a trusted Workspace reference."""
        return self._environment.get(name)

    @cached_property
    def runtime(self) -> RuntimeSettings:
        from .runtime import RuntimeSettings

        return RuntimeSettings()

    @cached_property
    def auth(self) -> AuthConfig:
        from qwenpaw_data_auth.config import AuthConfig

        return AuthConfig.from_env()

    @cached_property
    def cron(self) -> CronSettings:
        from .cron import CronSettings

        return CronSettings.from_environment()


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


def clear_settings() -> None:
    get_settings.cache_clear()


__all__ = ["Settings", "clear_settings", "get_settings"]
