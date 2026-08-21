# -*- coding: utf-8 -*-
from __future__ import annotations

import asyncio
import json
import logging
import os
import shlex
import tempfile
import uuid
from collections.abc import AsyncGenerator, Awaitable, Callable
from importlib.metadata import PackageNotFoundError, distribution
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from agentscope.message import TextBlock, ToolResultState
from agentscope.tool import Bash, ToolBase, ToolChunk
from agentscope.workspace import LocalWorkspace

from ..mcp_cm import is_cm_mcp_config
from ..paths import Paths
from ..tools.workspace import (
    WorkspaceBash,
    WorkspaceEdit,
    WorkspaceGlob,
    WorkspaceGrep,
    WorkspaceRead,
    WorkspaceWrite,
)

logger = logging.getLogger(__name__)

_DOCKER_SUPERVISOR_SCRIPT = """
import os
import pathlib
import subprocess
import sys

pidfile = pathlib.Path(sys.argv[1])
process = subprocess.Popen(sys.argv[2:], start_new_session=True)
pidfile.write_text(str(process.pid), encoding="ascii")
try:
    status = process.wait()
finally:
    try:
        pidfile.unlink()
    except FileNotFoundError:
        pass
raise SystemExit(status)
""".strip()

_DOCKER_REAPER_SCRIPT = """
import os
import pathlib
import signal
import sys
import time

pidfile = pathlib.Path(sys.argv[1])
grace = float(sys.argv[2])
ready_deadline = time.monotonic() + 0.75
while not pidfile.exists() and time.monotonic() < ready_deadline:
    time.sleep(0.02)
if not pidfile.exists():
    raise SystemExit(0)
try:
    pid = int(pidfile.read_text(encoding="ascii").strip())
except (OSError, ValueError):
    raise SystemExit(2)

def alive():
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False

try:
    os.killpg(pid, signal.SIGTERM)
except ProcessLookupError:
    pass
deadline = time.monotonic() + grace
while alive() and time.monotonic() < deadline:
    time.sleep(0.05)
if alive():
    try:
        os.killpg(pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
kill_deadline = time.monotonic() + 1.0
while alive() and time.monotonic() < kill_deadline:
    time.sleep(0.02)
if alive():
    raise SystemExit(3)
try:
    pidfile.unlink()
except FileNotFoundError:
    pass
""".strip()


class ManagedDockerBash(Bash):
    """Add process-group cleanup to AgentScope's public Docker Bash tool.

    AgentScope 2.0.3 cancels its wait on Docker exec timeout but leaves the
    process running in the container.  This compatibility adapter launches
    each command in its own session, records its process-group id, and reaps
    that group on timeout or cancellation.  It wraps the Bash tool returned by
    ``DockerWorkspace.list_tools()`` and does not access AgentScope internals.
    """

    def __init__(
        self,
        delegate: Bash,
        *,
        cleanup_failure_handler: Callable[[], Awaitable[None]] | None = None,
        grace_seconds: float = 3.0,
    ) -> None:
        super().__init__()
        self._delegate = delegate
        self._cleanup_failure_handler = cleanup_failure_handler
        self._grace_seconds = grace_seconds

    async def call(
        self,
        command: str,
        description: str = "",
        timeout: int = 120000,
    ) -> AsyncGenerator[ToolChunk, None]:
        pidfile = f"/tmp/qwenpaw-data-exec-{uuid.uuid4().hex}.pid"
        supervised = shlex.join(
            [
                "python",
                "-c",
                _DOCKER_SUPERVISOR_SCRIPT,
                pidfile,
                "/bin/sh",
                "-c",
                command,
            ],
        )
        chunks: list[ToolChunk] = []
        try:
            async for chunk in self._delegate.call(
                command=supervised,
                description=description,
                timeout=timeout,
            ):
                chunks.append(chunk)
        except asyncio.CancelledError:
            await self._reap_or_fail_closed(pidfile)
            raise

        if self._timed_out(chunks):
            await self._reap_or_fail_closed(pidfile)
            yield ToolChunk(
                content=[
                    TextBlock(
                        text=f"Command timed out after {min(timeout, 600000)}ms: "
                        f"{command}",
                    ),
                ],
                state=ToolResultState.ERROR,
                is_last=True,
            )
            return

        for chunk in chunks:
            yield chunk

    @staticmethod
    def _timed_out(chunks: list[ToolChunk]) -> bool:
        for chunk in chunks:
            if chunk.state != ToolResultState.ERROR:
                continue
            text = "".join(getattr(block, "text", "") for block in chunk.content)
            if "Command timed out after" in text:
                return True
        return False

    async def _reap_or_fail_closed(
        self,
        pidfile: str,
    ) -> None:
        cleanup = asyncio.create_task(
            self._run_cleanup(pidfile),
        )
        try:
            ok, error = await asyncio.shield(cleanup)
        except asyncio.CancelledError:
            ok, error = await cleanup
        except Exception:
            logger.error(
                "failed to reap interrupted Docker command",
                exc_info=True,
            )
            await self._fail_closed()
            return

        if not ok:
            logger.error(
                "failed to reap interrupted Docker command: %s",
                error,
            )
            await self._fail_closed()

    async def _run_cleanup(self, pidfile: str) -> tuple[bool, str]:
        command = shlex.join(
            [
                "python",
                "-c",
                _DOCKER_REAPER_SCRIPT,
                pidfile,
                str(self._grace_seconds),
            ],
        )
        chunks = [
            chunk
            async for chunk in self._delegate.call(
                command=command,
                description="Reap interrupted QwenPaw Data command",
                timeout=int((self._grace_seconds + 2.0) * 1000),
            )
        ]
        errors: list[str] = []
        for chunk in chunks:
            if chunk.state != ToolResultState.ERROR:
                continue
            errors.extend(getattr(block, "text", "") for block in chunk.content)
        return not errors, "\n".join(errors)

    async def _fail_closed(self) -> None:
        if self._cleanup_failure_handler is not None:
            await self._cleanup_failure_handler()


def _discover_skill_paths(skills_dir: Path) -> list[str]:
    if not skills_dir.is_dir():
        return []

    skill_files = [path for path in skills_dir.rglob("SKILL.md") if path.is_file()]
    return [
        str(path.parent)
        for path in sorted(
            skill_files,
            key=lambda path: path.relative_to(skills_dir).as_posix(),
        )
    ]


def _source_skill_paths() -> list[str]:
    package_root = Path(__file__).resolve().parents[6]
    return _discover_skill_paths(package_root / "qwenpaw-data-skills" / "skills")


def _installed_skill_paths() -> list[str]:
    try:
        dist = distribution("qwenpaw-data-skills")
    except PackageNotFoundError:
        return []
    return _discover_skill_paths(Path(dist.locate_file("qwenpaw_data_skills/skills")))


def _default_skill_paths() -> list[str]:
    return _source_skill_paths() or _installed_skill_paths()


# Docker workspace 预装的分析栈，避免每次任务在容器内临时安装。
_ANALYSIS_STACK = [
    "pandas",
    "numpy",
    "matplotlib",
    "openpyxl",
    "tabulate",
]


class QwenPawDataLocalWorkspace(LocalWorkspace):
    """Local workspace with QwenPaw Data workspace-scoped filesystem tools."""

    async def list_tools(self) -> list[ToolBase]:
        # 技能目录作为 Read 的额外只读根；写入类工具仅限 workspace 内
        skill_roots = list(getattr(self, "skill_paths", None) or [])
        # AgentScope 2.0.5 selects its public PowerShell tool and Windows-aware
        # LocalBackend on Windows. Keep QwenPaw Data's POSIX process-group adapter on
        # Unix, while delegating native Windows shell execution upstream.
        upstream_tools = await super().list_tools()
        shell_tool = (
            upstream_tools[0] if os.name == "nt" else WorkspaceBash(self.workdir)
        )
        return [
            shell_tool,
            WorkspaceEdit(self.workdir),
            WorkspaceGlob(self.workdir),
            WorkspaceGrep(self.workdir),
            WorkspaceRead(self.workdir, extra_roots=skill_roots),
            WorkspaceWrite(self.workdir),
        ]


def create_local_workspace(
    paths: Paths,
) -> QwenPawDataLocalWorkspace:
    """Create a LocalWorkspace backed by the host filesystem."""
    workdir = paths.workspace
    workdir.mkdir(parents=True, exist_ok=True)
    return QwenPawDataLocalWorkspace(
        workdir=str(workdir),
        skill_paths=_default_skill_paths(),
    )


def create_docker_workspace(
    paths: Paths,
) -> Any:
    """Create a DockerWorkspace backed by a mounted host workdir.

    镜像与依赖可通过环境变量调整：
    - ``QWENPAW_DATA_DOCKER_BASE_IMAGE``：基础镜像（默认 python:3.11-slim）
    - ``QWENPAW_DATA_DOCKER_EXTRA_PIP``：逗号分隔，追加在内置分析栈之后
    - ``QWENPAW_DATA_DOCKER_HOST_ALIAS``：容器内访问宿主机服务的主机名
      （默认 host.docker.internal）
    """
    extra_pip = list(_ANALYSIS_STACK)
    # 容器内 gateway 的运行依赖兼容性：上游镜像以 --no-deps 安装
    # agentscope（依赖树缺失，运行期陆续报 ModuleNotFoundError），且
    # requirements 未钉 mcp 版本（容易装到与宿主机 Tool 序列化不兼容的
    # 新版）。这里在 requirements 阶段先行安装带完整依赖树的
    # agentscope==宿主机版本，并将 mcp 钉到宿主机同版本。
    try:
        from importlib.metadata import version as _pkg_version

        extra_pip.append(f"agentscope=={_pkg_version('agentscope')}")
        extra_pip.append(f"mcp=={_pkg_version('mcp')}")
    except PackageNotFoundError:
        pass
    for name in os.getenv("QWENPAW_DATA_DOCKER_EXTRA_PIP", "").split(","):
        name = name.strip()
        if name and name not in extra_pip:
            extra_pip.append(name)

    workdir = paths.workspace
    workdir.mkdir(parents=True, exist_ok=True)
    return QwenPawDataDockerWorkspace(
        base_image=os.getenv("QWENPAW_DATA_DOCKER_BASE_IMAGE", "").strip()
        or "python:3.11-slim",
        host_workdir=str(workdir),
        node_version="20",
        extra_pip=extra_pip,
        skill_paths=_default_skill_paths(),
    )


_LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "[::1]", "::1"}


def _rewrite_loopback_url(url: str, alias: str) -> str:
    """将 loopback 主机名替换为容器可达的宿主机别名，其余 URL 原样返回。"""
    parts = urlsplit(url)
    if parts.hostname is None or parts.hostname.lower() not in _LOOPBACK_HOSTS:
        return url
    netloc = alias if parts.port is None else f"{alias}:{parts.port}"
    if parts.username:
        cred = parts.username + (f":{parts.password}" if parts.password else "")
        netloc = f"{cred}@{netloc}"
    return urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))


def _rewrite_mcp_specs(specs: list[dict[str, Any]], alias: str) -> list[dict[str, Any]]:
    """Return a JSON-compatible copy with loopback HTTP MCP URLs rewritten."""
    rewritten = json.loads(json.dumps(specs))
    for spec in rewritten:
        config = spec.get("mcp_config") if isinstance(spec, dict) else None
        if not isinstance(config, dict):
            continue
        url = config.get("url")
        if isinstance(url, str) and url:
            config["url"] = _rewrite_loopback_url(url, alias)
    return rewritten


def _container_mcp_specs(
    specs: list[dict[str, Any]],
    alias: str,
) -> list[dict[str, Any]]:
    """Build the runtime-only MCP configuration consumed inside Docker.

    Loopback URLs are replaced with the container's host alias.  The CM bearer
    credential is injected before AgentScope starts its gateway, because the
    gateway connects upstream during ``DockerWorkspace.initialize``.  This
    copy is temporary and must never replace the host's persisted ``.mcp``.
    """
    prepared = _rewrite_mcp_specs(specs, alias)
    api_token = (os.environ.get("QWENPAW_DATA_CLIENT_API_TOKEN") or "").strip() or (
        os.environ.get("QWENPAW_DATA_API_TOKEN") or ""
    ).strip()
    if not api_token:
        return prepared

    for spec in prepared:
        config = spec.get("mcp_config") if isinstance(spec, dict) else None
        if not isinstance(config, dict) or not is_cm_mcp_config(config):
            continue
        # Only auto-inject the local CM credential into the loopback URL that
        # was just rewritten to Docker's host alias.  A remote MCP endpoint
        # with a lookalike path must never receive this process-level secret.
        hostname = urlsplit(str(config.get("url") or "")).hostname or ""
        if hostname.lower() != alias.lower():
            continue
        headers = config.get("headers")
        if not isinstance(headers, dict):
            headers = {}
            config["headers"] = headers
        headers["Authorization"] = f"Bearer {api_token}"
    return prepared


def _write_json_atomic(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(value, handle, indent=2, ensure_ascii=False)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, path)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except FileNotFoundError:
            pass
        raise


def _make_qwenpaw_data_docker_workspace_cls() -> type:
    """延迟构造 DockerWorkspace 子类，避免 import 时引入 aiodocker 依赖。"""
    from agentscope.workspace import DockerWorkspace

    class _QwenPawDataDockerWorkspace(DockerWorkspace):
        """Docker workspace with host-safe MCP persistence.

        Only AgentScope's public lifecycle and MCP methods are overridden. The
        host ``.mcp`` file keeps loopback URLs for local tooling; a temporary
        container-facing copy is installed while the public ``initialize``
        method loads its configuration.
        """

        def _host_mcp_path(self) -> Path | None:
            host_workdir = getattr(self, "host_workdir", None)
            return Path(host_workdir) / ".mcp" if host_workdir else None

        @staticmethod
        def _alias() -> str:
            return (
                os.getenv("QWENPAW_DATA_DOCKER_HOST_ALIAS", "").strip()
                or "host.docker.internal"
            )

        async def initialize(self) -> None:
            path = self._host_mcp_path()
            original: list[dict[str, Any]] | None = None
            if path is not None and path.is_file():
                try:
                    value = json.loads(path.read_text(encoding="utf-8"))
                    if isinstance(value, list):
                        original = value
                        _write_json_atomic(
                            path,
                            _container_mcp_specs(original, self._alias()),
                        )
                except (OSError, ValueError, TypeError):
                    # Preserve upstream behaviour for malformed files: its
                    # public initialize method logs and falls back to defaults.
                    original = None
            try:
                await super().initialize()
            finally:
                if path is not None and original is not None:
                    _write_json_atomic(path, original)

        async def list_tools(self) -> list[ToolBase]:
            tools = await super().list_tools()
            return [
                ManagedDockerBash(
                    tool,
                    cleanup_failure_handler=self._close_after_cleanup_failure,
                )
                if isinstance(tool, Bash)
                else tool
                for tool in tools
            ]

        async def _close_after_cleanup_failure(self) -> None:
            logger.critical(
                "Docker command cleanup failed; closing the workspace "
                "container to fail closed",
            )
            await self.close()

        async def add_mcp(self, mcp_client: Any) -> None:
            from agentscope.mcp import MCPClient

            path = self._host_mcp_path()
            host_specs = self._read_host_specs(path)
            host_spec = mcp_client.model_dump(mode="json")
            docker_spec = _container_mcp_specs([host_spec], self._alias())[0]
            await super().add_mcp(MCPClient.model_validate(docker_spec))
            if path is not None:
                host_specs.append(host_spec)
                _write_json_atomic(path, host_specs)

        async def remove_mcp(self, name: str) -> None:
            path = self._host_mcp_path()
            host_specs = self._read_host_specs(path)
            await super().remove_mcp(name)
            if path is not None:
                _write_json_atomic(
                    path,
                    [spec for spec in host_specs if spec.get("name") != name],
                )

        @staticmethod
        def _read_host_specs(path: Path | None) -> list[dict[str, Any]]:
            if path is None or not path.is_file():
                return []
            value = json.loads(path.read_text(encoding="utf-8"))
            return value if isinstance(value, list) else []

    return _QwenPawDataDockerWorkspace


def QwenPawDataDockerWorkspace(**kwargs: Any) -> Any:  # noqa: N802
    """工厂函数形态的入口，保持调用侧类似构造器的用法。"""
    cls = _make_qwenpaw_data_docker_workspace_cls()
    return cls(**kwargs)
