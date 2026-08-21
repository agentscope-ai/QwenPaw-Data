# -*- coding: utf-8 -*-
from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path

from .artifact_paths import ArtifactPathContext

QWENPAW_DATA_HOME_ENV = "QWENPAW_DATA_HOME"
_DEFAULT_HOME = Path("~/.qwenpaw-data")


def resolve_qwenpaw_data_home(home: str | Path | None = None) -> Path:
    """解析 QwenPaw Data 根目录。

    优先级：显式参数 > ``QWENPAW_DATA_HOME`` 环境变量 > 默认 ``~/.qwenpaw-data``。
    """
    if home is not None:
        candidate = Path(home)
    else:
        raw = (os.environ.get(QWENPAW_DATA_HOME_ENV) or "").strip()
        candidate = Path(raw) if raw else _DEFAULT_HOME
    return candidate.expanduser().resolve()


def host_root(home: str | Path | None = None) -> Path:
    """解析 Host 顶层状态目录。"""
    return resolve_qwenpaw_data_home(home) / "host"


@dataclass(frozen=True)
class Paths:
    """绑定到 ``(home, session_id)`` 的 Host 路径视图。"""

    home: Path
    session_id: str = "default"

    def __post_init__(self) -> None:
        object.__setattr__(self, "home", resolve_qwenpaw_data_home(self.home))

    @property
    def host_root(self) -> Path:
        return host_root(self.home)

    @property
    def secrets_root(self) -> Path:
        return self.host_root / ".secrets"

    @property
    def session_root(self) -> Path:
        """AgentScope 当前 session 的上下文与工具结果目录。"""
        return self.sessions_root / self.session_id

    @property
    def workspace(self) -> Path:
        return self.host_root / "workspace"

    @property
    def mcp_config_file(self) -> Path:
        return self.workspace / ".mcp"

    @property
    def artifacts_root(self) -> Path:
        return self.workspace / "artifacts"

    @property
    def skills(self) -> Path:
        return self.workspace / "skills"

    @property
    def sessions_root(self) -> Path:
        return self.workspace / "sessions"

    @property
    def console_root(self) -> Path:
        return self.sessions_root / "console"

    @property
    def dag_root(self) -> Path:
        return self.sessions_root / "dag"

    @property
    def data_root(self) -> Path:
        """AgentScope 内容寻址的共享多模态数据目录。"""
        return self.workspace / "data"

    @property
    def artifact_dir(self) -> Path:
        """该 session 的产物根目录。"""
        return self.artifacts_root / self.session_id

    def node_artifact_dir(self, graph_id: str, node_id: str) -> Path:
        """解析当前 session 下的 graph/node 产物目录。"""
        return self.artifact_dir / graph_id / node_id

    @property
    def artifact_context(self) -> ArtifactPathContext:
        """该 session 的产物路径上下文。"""
        return ArtifactPathContext(self.artifact_dir)
