"""Prepare workspace rebuilds using Runtime-bound authority and shared recovery."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable

from qwenpaw_data_auth.authorization import AuthorizationContext

from ..auth import AuthorizationError, ScopeBinding, active_target
from ..auth.repository import OwnerRepository
from ..executor import BoundedExecutor
from ..model import ModelConfigError, ModelService
from ..store import GraphStore
from ..transaction.recovery import RecoveryService
from .manager import EmbeddingJobManager, RebuildWork
from .rebuild import embed_all
from .repository import rebuild_rid

log = logging.getLogger(__name__)


class EmbeddingDimensionMismatch(ValueError):
    """The selected model cannot populate the Store's vector properties."""


class EmbeddingRebuildService:
    """Use the current requester's clients to rebuild one entire workspace."""

    def __init__(
        self,
        store: GraphStore,
        models: Callable[[str], ModelService],
        jobs: EmbeddingJobManager,
        *,
        executor: BoundedExecutor,
        recovery: RecoveryService,
        scopes_for: Callable[[], tuple[ScopeBinding, Callable[[str], ScopeBinding]]],
    ) -> None:
        self.jobs = jobs
        self._store = store
        self._models = models
        self._executor = executor
        self._recovery = recovery
        self._scopes_for = scopes_for

    async def start(
        self, context: AuthorizationContext, *, job_id: str | None = None
    ) -> str:
        owner = active_target(context)
        if job_id is not None:
            job = await self.jobs.get(owner, job_id)
            if job is None:
                raise LookupError("embedding job not found")
            if job["status"] != "failed":
                raise ValueError("only failed embedding jobs can be retried")
        scope, write_scope_for_owner = await self._executor.run(self._scopes_for)
        await self._recovery.require_ready(scope)
        snapshot, work = await self._prepare(scope, write_scope_for_owner)
        return await self.jobs.submit(
            owner, snapshot, work, created_by=scope.user_id, job_id=job_id
        )

    async def resume_claimed(self, job: dict) -> None:
        """Rerun one rebuild that this process just claimed at startup."""
        scope, write_scope_for_owner = await self._executor.run(self._scopes_for)
        await self._recovery.require_ready(scope)
        _snapshot, work = await self._prepare(scope, write_scope_for_owner)
        self.jobs.adopt(
            (job["tenant_id"], job["workspace_id"]),
            job["job_id"],
            int(job["resume_count"]),
            work,
            rebuild_rid(job["job_id"], job["attempted_at"]),
            None,
        )

    @staticmethod
    def _resolve_initiator(
        tenant_id: str,
        workspace_id: str,
        user_id: str,
    ) -> AuthorizationContext:
        """Reload the original initiator's current access before a startup resume."""
        bindings = OwnerRepository().list_workspace_roles(
            tenant_id, workspace_id, user_id
        )
        roles = {
            row["role"]
            for row in bindings
            if (row["scope_type"] == "tenant" and row["role"] == "tenant_admin")
            or (row["scope_type"] == "workspace" and row["role"] == "ws_admin")
        }
        if not roles:
            raise AuthorizationError(
                "rebuild initiator is no longer a workspace administrator"
            )
        role = "tenant_admin" if "tenant_admin" in roles else "ws_admin"
        return AuthorizationContext(
            user_id=user_id,
            tenant_id=tenant_id,
            role=role,
            active_workspace_role=role,
            active_workspace_id=workspace_id,
            workspace_ids=frozenset({workspace_id}),
            admin_workspace_ids=frozenset({workspace_id}),
        )

    async def _prepare(
        self, scope: ScopeBinding, write_scope_for_owner: Callable[[str], ScopeBinding]
    ) -> tuple[dict, RebuildWork]:
        store = self._store
        clients = await self._executor.run(self._models, scope.user_id)
        config = await self._executor.run(clients.resolve_for_use, "embedding")
        dim = config.dim
        dimensions = store.embedding_dimensions
        if dim is None:
            raise ModelConfigError("save an embedding dimension before rebuilding")
        if dimensions and dimensions != {dim}:
            raise EmbeddingDimensionMismatch(
                "embedding dimension must match the Store schema"
            )
        snapshot = {
            "model": config.model,
            "base_url": config.base_url,
            "dim": config.dim,
        }

        async def work(progress, job_id, rid, previous_rid) -> int:
            async with (
                self._recovery.run(
                    scope,
                    task_type="embedding",
                    task_id=job_id,
                    rid=rid,
                    previous_rid=previous_rid,
                ) as recovery_session,
                clients.embedder(config) as embedder,
            ):
                return await embed_all(
                    store,
                    scope,
                    embedder,
                    executor=self._executor,
                    on_progress=progress,
                    write_scope_for_owner=write_scope_for_owner,
                    recovery_session=recovery_session,
                )

        return snapshot, work


async def resume_expired_rebuilds(
    jobs: EmbeddingJobManager,
    open_service: Callable[[AuthorizationContext], Awaitable[EmbeddingRebuildService]],
) -> None:
    """Claim abandoned rebuilds and rerun the ones this process won.

    Permission lookup and failure settlement stay in this module. Runtime only
    starts the pass after the database is ready.
    """
    claimed = await asyncio.to_thread(jobs._repository.claim_stale)
    for job in claimed:
        try:
            context = await asyncio.to_thread(
                EmbeddingRebuildService._resolve_initiator,
                job["tenant_id"],
                job["workspace_id"],
                job["created_by"],
            )
            service = await open_service(context)
            await service.resume_claimed(job)
        except Exception:
            log.exception(
                "stale embedding rebuild %s could not be resumed", job.get("job_id")
            )
            await asyncio.to_thread(
                jobs._repository.finish,
                (job["tenant_id"], job["workspace_id"]),
                job["job_id"],
                int(job["resume_count"]),
                status="failed",
                progress={**(job.get("progress") or {}), "phase": "failed"},
                error="执行中断",
            )


def public_job(job: dict | None) -> dict | None:
    """Keep durable identity and attempt metadata out of the frontend contract."""
    if job is None:
        return None
    return {
        name: job[name]
        for name in (
            "job_id",
            "status",
            "created_at",
            "started_at",
            "finished_at",
            "error",
            "progress",
            "config_snapshot",
        )
    }
