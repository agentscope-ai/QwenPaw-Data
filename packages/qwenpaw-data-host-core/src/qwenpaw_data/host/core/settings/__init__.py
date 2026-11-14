# -*- coding: utf-8 -*-
from __future__ import annotations

from functools import lru_cache

from qwenpaw_data.host.core.settings.catalog import CatalogSettings
from qwenpaw_data.host.core.settings.clarification import ClarificationSettings
from qwenpaw_data.host.core.settings.cm import CmSettings
from qwenpaw_data.host.core.settings.compare import CompareSettings
from qwenpaw_data.host.core.settings.cron import CronSettings
from qwenpaw_data.host.core.settings.db import DbSettings
from qwenpaw_data.host.core.settings.fallback import FallbackSettings
from qwenpaw_data.host.core.settings.followup import FollowUpSettings
from qwenpaw_data.host.core.settings.log import LogSettings
from qwenpaw_data.host.core.settings.mcp import McpSettings
from qwenpaw_data.host.core.settings.tracing import TracingSettings
from qwenpaw_data.host.core.settings.oss import OssSettings
from qwenpaw_data.host.core.settings.paths import PathsSettings
from qwenpaw_data.host.core.settings.runtime import RuntimeSettings
from qwenpaw_data.host.core.settings.security import SecuritySettings
from qwenpaw_data.host.core.settings.stream import StreamSettings
from qwenpaw_data.host.core.settings.tool_confirmation import ToolConfirmationSettings
from qwenpaw_data.host.core.settings.tools import ToolSettings
from qwenpaw_data.host.core.settings.workspace import WorkspaceSettings


class Settings:
    def __init__(
        self,
        *,
        db: DbSettings | None = None,
        paths: PathsSettings | None = None,
        catalog: CatalogSettings | None = None,
        cm: CmSettings | None = None,
        compare: CompareSettings | None = None,
        cron: CronSettings | None = None,
        runtime: RuntimeSettings | None = None,
        clarification: ClarificationSettings | None = None,
        tool_confirmation: ToolConfirmationSettings | None = None,
        tools: ToolSettings | None = None,
        security: SecuritySettings | None = None,
        fallback: FallbackSettings | None = None,
        followup: FollowUpSettings | None = None,
        log: LogSettings | None = None,
        mcp: McpSettings | None = None,
        stream: StreamSettings | None = None,
        workspace: WorkspaceSettings | None = None,
        oss: OssSettings | None = None,
        tracing: TracingSettings | None = None,
    ) -> None:
        self.db = db or DbSettings()
        self.paths = paths or PathsSettings()
        self.catalog = catalog or CatalogSettings()
        self.cm = cm or CmSettings()
        self.compare = compare or CompareSettings()
        self.cron = cron or CronSettings()
        self.runtime = runtime or RuntimeSettings()
        self.clarification = clarification or ClarificationSettings()
        self.tool_confirmation = tool_confirmation or ToolConfirmationSettings()
        self.tools = tools or ToolSettings()
        self.security = security or SecuritySettings()
        self.fallback = fallback or FallbackSettings()
        self.followup = followup or FollowUpSettings()
        self.log = log or LogSettings()
        self.mcp = mcp or McpSettings()
        self.stream = stream or StreamSettings()
        self.workspace = workspace or WorkspaceSettings()
        if self.workspace.is_cloud_sandbox:
            self.cm.sandbox_mcp_url()
        self.oss = oss or OssSettings()
        self.tracing = tracing or TracingSettings()


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


def clear_settings() -> None:
    get_settings.cache_clear()


__all__ = [
    "Settings",
    "CatalogSettings",
    "DbSettings",
    "PathsSettings",
    "CmSettings",
    "CompareSettings",
    "CronSettings",
    "RuntimeSettings",
    "ClarificationSettings",
    "ToolConfirmationSettings",
    "ToolSettings",
    "SecuritySettings",
    "FallbackSettings",
    "FollowUpSettings",
    "LogSettings",
    "McpSettings",
    "StreamSettings",
    "WorkspaceSettings",
    "OssSettings",
    "TracingSettings",
    "get_settings",
    "clear_settings",
]
