# -*- coding: utf-8 -*-
from __future__ import annotations

import asyncio
import hashlib
import logging
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any
from weakref import WeakValueDictionary

from agentscope.workspace import LocalWorkspace

from qwenpaw_data.host.core.paths import UserAgentPaths
from qwenpaw_data.host.core.repositories.skill_store import SkillStoreRepo
from qwenpaw_data.host.core.settings import get_settings

logger = logging.getLogger(__name__)


@dataclass
class _WorkspaceEntry:
    workspace: Any
    idle_since: float
    users: int = 0


def stable_workspace_id(paths: UserAgentPaths) -> str:
    """Short id for cache / Docker name: tenant+workspace+user+agent."""
    raw = f"{paths.tenant_id}\0{paths.workspace_id}\0{paths.user_id}\0{paths.agent_id}"
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def prepare_workspace_dirs(paths: UserAgentPaths) -> None:
    from qwenpaw_data.host.core.runtime.builder import get_agent_builder

    paths.workspace.mkdir(parents=True, exist_ok=True)
    get_agent_builder(paths.agent_id).prepare_workspace(SkillStoreRepo(paths.skills))


class QwenPawDataWorkspaceManager:
    """Share Local/Docker/cloud workspaces across sessions and manage their lifetime."""

    def __init__(self) -> None:
        self._cache: dict[str, _WorkspaceEntry] = {}
        self._workspace_locks: WeakValueDictionary[str, asyncio.Lock] = (
            WeakValueDictionary()
        )
        self._sweep_task: asyncio.Task[None] | None = None
        self._start_lock = asyncio.Lock()
        self._sweep_jobs: dict[str, asyncio.Task[None]] = {}
        self._stopping = False

    def _lock(self, workspace_id: str) -> asyncio.Lock:
        # Holders and waiters retain the lock; unused locks disappear automatically.
        return self._workspace_locks.setdefault(workspace_id, asyncio.Lock())

    async def start(self) -> None:
        async with self._start_lock:
            if self._stopping:
                raise RuntimeError("workspace manager is shutting down")
            if self._sweep_task is not None:
                return
            # Failed/cancelled warmup must not leave a running marker that lets
            # subsequent callers bypass initialization or leaks a sweeper.
            await self._warm_docker_image()
            if self._stopping:
                raise RuntimeError("workspace manager is shutting down")
            self._sweep_task = asyncio.create_task(self._sweep_loop())

    async def _warm_docker_image(self) -> None:
        settings = get_settings().workspace
        if settings.backend != "docker":
            return
        from agentscope.workspace import DockerWorkspace

        logger.info(
            "warming docker workspace image (base=%s)",
            settings.docker_base_image,
        )
        ws = DockerWorkspace(
            workspace_id="qwenpaw-data-warmup",
            host_workdir=None,
            base_image=settings.docker_base_image,
            node_version=settings.docker_node_version,
            extra_pip=settings.extra_pip_packages(),
            gateway_port=settings.docker_gateway_port,
        )
        try:
            await ws.initialize()
        finally:
            await self._close(ws)
        logger.info("docker workspace image ready")

    async def stop(self) -> None:
        """Stop maintenance and retire workspaces; active leases close on exit."""
        self._stopping = True
        task = self._sweep_task
        self._sweep_task = None
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        jobs = list(self._sweep_jobs.values())
        for job in jobs:
            job.cancel()
        await asyncio.gather(*jobs, return_exceptions=True)
        await self.close_all()

    async def _acquire(
        self, paths: UserAgentPaths, workspace_id: str
    ) -> _WorkspaceEntry:
        async with self._lock(workspace_id):
            if self._stopping:
                raise RuntimeError("workspace manager is shutting down")
            entry = self._cache.get(workspace_id)
            ws = entry.workspace if entry is not None else None
            cache_hit = ws is not None and ws.is_alive
            if cache_hit and hasattr(ws, "running"):
                cache_hit = await ws.running()
            if cache_hit:
                # Check/renew before handing out a lease close to the cloud timeout.
                if hasattr(ws, "renew"):
                    try:
                        await ws.renew()
                    except Exception:
                        await self._retire(workspace_id)
                        raise
            else:
                if ws is not None:
                    await self._retire(workspace_id)
                ws = await self._create_workspace(paths, workspace_id)
                entry = _WorkspaceEntry(ws, time.monotonic())
                self._cache[workspace_id] = entry
            entry.users += 1
            logger.info(
                "workspace_acquire workspace_id=%s cache_hit=%s",
                workspace_id,
                str(cache_hit).lower(),
            )
            return entry

    async def _create_workspace(self, paths: UserAgentPaths, workspace_id: str) -> Any:
        settings = get_settings().workspace
        if settings.backend == "local":
            ws: Any = LocalWorkspace(
                workdir=str(paths.workspace),
                workspace_id=workspace_id,
            )
        elif settings.backend == "docker":
            from agentscope.workspace import DockerWorkspace

            ws = DockerWorkspace(
                workspace_id=workspace_id,
                host_workdir=str(paths.workspace),
                base_image=settings.docker_base_image,
                node_version=settings.docker_node_version,
                extra_pip=settings.extra_pip_packages(),
                gateway_port=settings.docker_gateway_port,
            )
        elif settings.backend == "fc-sandbox":
            from .fc_workspace import FcE2BWorkspace

            ws = FcE2BWorkspace(paths=paths, workspace_id=workspace_id)
        elif settings.backend == "acs-sandbox":
            from .acs_workspace import AcsE2BWorkspace

            ws = AcsE2BWorkspace(paths=paths, workspace_id=workspace_id)
        else:
            raise ValueError(f"unsupported workspace backend: {settings.backend!r}")
        try:
            await asyncio.to_thread(prepare_workspace_dirs, paths)
            await ws.initialize()
            if self._stopping:
                raise RuntimeError("workspace manager is shutting down")
        except BaseException:
            await self._close(ws)
            raise
        return ws

    @asynccontextmanager
    async def lease(self, paths: UserAgentPaths):
        """Keep the instance alive until its last user exits, even after eviction."""
        if self._stopping:
            raise RuntimeError("workspace manager is shutting down")
        workspace_id = stable_workspace_id(paths)
        await self.start()
        entry = await self._acquire(paths, workspace_id)
        try:
            yield entry.workspace
        finally:
            async with self._lock(workspace_id):
                entry.users -= 1
                if entry.users == 0:
                    ws = entry.workspace
                    if self._cache.get(workspace_id) is not entry:
                        await self._close(ws)
                    else:
                        entry.idle_since = time.monotonic()
                        if hasattr(ws, "renew"):
                            try:
                                await ws.renew(idle=True)
                            except Exception:
                                logger.exception(
                                    "workspace_idle_renew_failed workspace_id=%s",
                                    workspace_id,
                                )
                                await self._retire(workspace_id)

    async def purge_session(
        self,
        paths: UserAgentPaths,
        *,
        session_id: str,
    ) -> None:
        workspace_id = stable_workspace_id(paths)
        async with self._lock(workspace_id):
            cached = self._cache.get(workspace_id)
            if cached is not None:
                await cached.workspace.purge_session(
                    agent_id=paths.agent_id,
                    session_id=session_id,
                )

    async def close_all(self) -> None:
        for wid in list(self._cache):
            async with self._lock(wid):
                if wid in self._cache:
                    await self._retire(wid)

    async def _retire(self, workspace_id: str) -> None:
        """Evict under the lifecycle lock; active users close the instance on exit."""
        entry = self._cache[workspace_id]
        if entry.users == 0:
            await self._close(entry.workspace)
        self._cache.pop(workspace_id)

    async def _sweep_loop(self) -> None:
        while True:
            try:
                cfg = get_settings().workspace
                interval = (
                    min(cfg.sweep_interval, cfg.sandbox.renew_interval_seconds)
                    if cfg.is_cloud_sandbox
                    else cfg.sweep_interval
                )
                await asyncio.sleep(interval)
            except asyncio.CancelledError:
                return
            self._schedule_sweep()

    def _schedule_sweep(self) -> None:
        # A slow lifecycle lock or RPC must not delay another workspace's next tick.
        for wid in self._cache:
            if wid not in self._sweep_jobs:
                self._sweep_jobs[wid] = asyncio.create_task(self._sweep_workspace(wid))

    async def _sweep_workspace(self, wid: str) -> None:
        cfg = get_settings().workspace
        ttl = cfg.sandbox.idle_ttl_seconds if cfg.is_cloud_sandbox else cfg.ttl
        try:
            async with self._lock(wid):
                entry = self._cache.get(wid)
                if entry is None:
                    return
                ws = entry.workspace
                expired = (
                    entry.users == 0 and time.monotonic() - entry.idle_since >= ttl
                )
                if entry.users and hasattr(ws, "renew"):
                    try:
                        await ws.renew()
                    except Exception:
                        logger.exception("workspace_renew_failed workspace_id=%s", wid)
                        expired = True
                if expired:
                    await self._retire(wid)
                    logger.info("workspace_evicted workspace_id=%s", wid)
        except Exception:
            logger.exception("workspace sweep failed workspace_id=%s", wid)
        finally:
            self._sweep_jobs.pop(wid, None)

    @staticmethod
    async def _close(ws: Any) -> None:
        try:
            await ws.close()
        except Exception:
            logger.exception(
                "failed to close workspace %s",
                getattr(ws, "workspace_id", "<unknown>"),
            )


_manager: QwenPawDataWorkspaceManager | None = None


def get_workspace_manager() -> QwenPawDataWorkspaceManager:
    global _manager
    if _manager is None:
        _manager = QwenPawDataWorkspaceManager()
    return _manager


async def stop_workspace_manager() -> None:
    global _manager
    if _manager is None:
        return
    await _manager.stop()
    _manager = None
