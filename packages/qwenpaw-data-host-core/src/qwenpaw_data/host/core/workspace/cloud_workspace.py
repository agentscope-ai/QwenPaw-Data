"""E2B-compatible cloud workspace with the user's OSS directory at /qwenpaw_data.

Cloud sandboxes use kill/recreate; the Host manager owns identity and lifetime.
"""

from __future__ import annotations

import logging
from time import perf_counter

from agentscope.workspace import E2BBackend, E2BWorkspace

from qwenpaw_data.host.core.paths import UserAgentPaths
from qwenpaw_data.host.core.settings import get_settings

logger = logging.getLogger(__name__)


def user_oss_path(paths: UserAgentPaths) -> str:
    """OSS key prefix of the user's directory, without a leading slash."""
    oss = get_settings().workspace.oss
    return "/".join((oss.prefix, paths.tenant_id, paths.workspace_id, paths.user_id))


class CloudE2BWorkspace(E2BWorkspace):
    def __init__(
        self,
        *,
        paths: UserAgentPaths,
        workspace_id: str,
        template: str,
        api_key: str,
        domain: str,
        metadata: dict[str, str],
    ) -> None:
        super().__init__(
            workspace_id=workspace_id,
            template=template,
            api_key=api_key,
            domain=domain,
            timeout_seconds=get_settings().workspace.sandbox.timeout_seconds,
            env={
                "PATH": f"{self._gateway_home}/.venv/bin:"
                "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
            },
            sandbox_metadata={"qwenpaw_data.workspace.kind": "user", **metadata},
        )
        self.paths = paths
        self.workdir = f"/qwenpaw_data/{paths.agent_id}/workspace"

    async def _find_existing_sandbox(self) -> None:
        """Cloud backends create fresh sandboxes; the Host manager shares active ones."""
        return None

    async def _provision_backend(self) -> None:
        await super()._provision_backend()
        # The SDK binds a fixed default directory; use the agent's OSS workspace.
        self._backend = E2BBackend(self._sandbox, self.workdir)

    async def initialize(self) -> None:
        if self.is_alive:
            return
        started, outcome = perf_counter(), "failed"
        logger.info(
            "sandbox_startup_started workspace_id=%s template=%s",
            self.workspace_id,
            self.template,
        )
        try:
            await super().initialize()
            outcome = "ok"
        finally:
            logger.info(
                "sandbox_startup_finished workspace_id=%s sandbox_id=%s "
                "template=%s outcome=%s duration_ms=%.2f",
                self.workspace_id,
                self.sandbox_id,
                self.template,
                outcome,
                (perf_counter() - started) * 1000,
            )

    def _bootstrap_commands(self) -> list[str]:
        # Do not let the SDK install dependencies if a template is incomplete.
        raise RuntimeError("cloud sandboxes require a prebuilt sandbox template")

    async def running(self) -> bool:
        return (
            self.is_alive
            and self._sandbox is not None
            and await self._sandbox.is_running()
        )

    async def renew(self, *, idle: bool = False) -> None:
        cfg = get_settings().workspace.sandbox
        timeout = cfg.timeout_seconds
        if idle:
            timeout = max(timeout, cfg.idle_ttl_seconds + 60)
        await self._sandbox.set_timeout(timeout, request_timeout=10)

    async def _teardown_backend(self) -> None:
        if self._sandbox is not None:
            await self._sandbox.kill()
            self._sandbox = None

    async def get_instructions(self) -> str:
        return (
            f"所有命令和文件操作均在沙箱内执行，工作目录为 {self.workdir}。\n"
            "Shell 相对路径以工作目录为起点；Read、Write、Edit 必须使用沙箱内的绝对路径。\n"
            f"附件清单中的 uploads/... 对应 {self.workdir}/uploads/...。\n"
            "需要保留的结果写入当前 Session 产物目录，写入完成后关闭文件。\n"
            "共享 Skill/MCP 配置通过 Skill 草稿流程修改，不直接改写共享配置文件。"
        )
