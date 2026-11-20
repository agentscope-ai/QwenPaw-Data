"""Adapt incremental chat history to the memory ingestion service."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Mapping
from contextlib import asynccontextmanager
from datetime import UTC, datetime

from qwenpaw_data_auth.authorization import AuthorizationContext

from ..executor import BoundedExecutor
from ..model import ModelService
from ..model.usage import bind_usage_context
from ..store import GraphStore
from ..auth import WriteOwner
from ..auth.repository import OwnerRepository
from ..settings import get_settings
from .loader import ChatLoader, ChatLoaderCheckpointRepository, UserHistoryScope
from .ingest import MemoryIngestService, _utc_timestamp
from .repository import UserIngestRepository

_LOGGER = logging.getLogger(__name__)


class _LeaseLost(RuntimeError):
    """The user batch no longer owns its execution lease."""


class IngestAdapter:
    """Load pending history, ingest each user's messages, and acknowledge versions."""

    def __init__(
        self,
        store_for_user: Callable[[AuthorizationContext], GraphStore],
        models_for_user: Callable[[AuthorizationContext], ModelService],
        *,
        executor: BoundedExecutor,
        loader: ChatLoader | None = None,
    ) -> None:
        self._store_for_user = store_for_user
        self._models_for_user = models_for_user
        self._executor = executor
        self._loader = (
            loader
            if loader is not None
            else ChatLoader(
                ChatLoaderCheckpointRepository(),
            )
        )
        self._leases = UserIngestRepository()
        self._heartbeat_interval = get_settings().heartbeat.interval_seconds

    async def ingest(
        self,
        cutoff_at: datetime,
        target_scope: UserHistoryScope | None = None,
    ) -> Mapping[str, object]:
        """Process pending history up to the cutoff, optionally for one user."""
        cutoff = _utc_timestamp(cutoff_at)
        with bind_usage_context(
            source="用户图谱更新",
            source_step=f"user_graph_update#{cutoff.isoformat()}",
            session_id="",
            chat_id="",
            probe=False,
        ):
            loaded = await self._executor.run(
                self._loader.load_pending,
                cutoff_at=cutoff,
                target_scope=target_scope,
            )
            outcomes, messages = [], []
            for batch in loaded.batches:
                details = []
                outcomes.append(
                    await self._ingest_user(batch, loaded.evidence_times, details)
                )
                messages.extend(details)
        processed = sum(row["status"] == "success" for row in messages)
        failed = sum(row["status"] == "failed" for row in messages)
        source_failures = failed + sum(
            issue["status"] == "failed" for issue in loaded.issues
        )
        skipped = (
            loaded.messages_skipped
            + sum(row["status"] == "skipped" for row in messages)
            + sum(issue["status"] == "skipped" for issue in loaded.issues)
        )
        status = (
            "success"
            if not source_failures
            else ("partial_failed" if processed or skipped else "failed")
        )
        if (
            outcomes
            and all(outcome == "skipped" for outcome in outcomes)
            and not source_failures
        ):
            status = "skipped"
        return {
            "status": status,
            **({"reason": "overlap"} if status == "skipped" else {}),
            "users_discovered": len(loaded.batches),
            "users_succeeded": outcomes.count("success"),
            "users_failed": outcomes.count("failed"),
            "users_unauthorized": outcomes.count("unauthorized"),
            "messages_processed": processed,
            "messages_failed": failed,
            "sources": {
                "checked": loaded.messages_checked + len(loaded.issues),
                "succeeded": processed,
                "skipped": skipped,
                "failed": source_failures,
            },
            "details": [
                dict(issue) for issue in (*loaded.issues, *loaded.skipped_sources)
            ]
            + messages,
        }

    async def _ingest_user(self, batch, evidence_times, details) -> str:
        authorization = await asyncio.to_thread(
            self._history_authorization, batch.scope
        )
        if authorization is None:
            details.extend(
                {
                    "source_id": message.source_id,
                    "status": "failed",
                    "reason": "source_scope_unauthorized",
                }
                for message in batch.messages
            )
            return "unauthorized"
        try:
            async with self._lease(batch.scope) as token:
                if token is None:
                    details.extend(
                        {
                            "source_id": message.source_id,
                            "status": "skipped",
                            "reason": "overlap",
                        }
                        for message in batch.messages
                    )
                    return "skipped"
                return await self._process_user(
                    batch, authorization, evidence_times, details, token
                )
        except _LeaseLost:
            details.extend(
                {
                    "source_id": message.source_id,
                    "status": "failed",
                    "reason": "ingest_lease_lost",
                }
                for message in batch.messages[len(details) :]
            )
            return "failed"

    async def _process_user(
        self, batch, authorization, evidence_times, details, token: str
    ) -> str:
        try:
            service, read_scope, write_scope = await self._executor.run(
                self._prepare_user,
                authorization,
            )
        except Exception as exc:
            details.extend(
                {
                    "source_id": message.source_id,
                    "status": "failed",
                    "reason": "user_graph_setup_failed",
                    "error_type": type(exc).__name__,
                }
                for message in batch.messages
            )
            _LOGGER.exception("User chat ingest setup failed for %s", batch.scope)
            return "failed"
        user_failed = False
        for message in batch.messages:
            reason = "user_graph_update_failed"
            try:
                # Check again after claiming: another instance may have finished meanwhile.
                if await asyncio.to_thread(
                    self._loader.is_processed, batch.scope, message
                ):
                    details.append(
                        {
                            "source_id": message.source_id,
                            "status": "skipped",
                            "reason": "unchanged_source",
                        }
                    )
                    continue
                await service.ingest(
                    (*message.context, message.document()),
                    read_scope,
                    write_scope,
                    occurred_at=datetime.now(UTC),
                    target_message_id=message.message_id,
                    tool_observations=message.tool_observations,
                    **(
                        {"evidence_times": evidence_times[batch.scope]}
                        if batch.scope in evidence_times
                        else {}
                    ),
                )
                # TODO: Coordinate graph commit and checkpoint acknowledgement across stores.
                reason = "checkpoint_write_failed"
                if not await asyncio.to_thread(self._leases.renew, batch.scope, token):
                    raise _LeaseLost
                await asyncio.to_thread(
                    self._loader.acknowledge,
                    batch.scope,
                    message,
                    processed_at=datetime.now(UTC),
                )
                details.append({"source_id": message.source_id, "status": "success"})
            except Exception as exc:
                user_failed = True
                details.append(
                    {
                        "source_id": message.source_id,
                        "status": "failed",
                        "reason": reason,
                        "error_type": type(exc).__name__,
                    }
                )
                _LOGGER.exception(
                    "User chat ingest failed for source=%s", message.source_id
                )
        return "failed" if user_failed else "success"

    def _prepare_user(self, authorization: AuthorizationContext):
        """Bind one user's service and scopes before processing their messages."""
        store = self._store_for_user(authorization)
        read_scope = store.access.issue_read_scope(authorization)
        write_scope = store.access.issue_write_scope(
            authorization,
            owner=WriteOwner.USER,
            zone="user",
        )
        models = self._models_for_user(authorization)
        if not models.ready:
            raise RuntimeError("User Graph ingest model configuration is incomplete")
        return (
            MemoryIngestService(store, models, executor=self._executor),
            read_scope,
            write_scope,
        )

    @asynccontextmanager
    async def _lease(self, scope):
        token, heartbeat = None, None
        claim = asyncio.create_task(asyncio.to_thread(self._leases.claim, scope))
        try:
            try:
                token = await asyncio.shield(claim)
            except asyncio.CancelledError:
                token = await claim
                raise
            if token is None:
                yield None
                return
            lost = asyncio.Event()
            heartbeat = asyncio.create_task(
                self._renew(scope, token, lost, asyncio.current_task())
            )
            try:
                yield token
            except asyncio.CancelledError:
                if lost.is_set():
                    raise _LeaseLost from None
                raise
        finally:
            if heartbeat is not None:
                heartbeat.cancel()
                await asyncio.gather(heartbeat, return_exceptions=True)
            if token is not None:
                await asyncio.to_thread(self._leases.release, scope, token)

    async def _renew(self, scope, token, lost, worker):
        while True:
            await asyncio.sleep(self._heartbeat_interval)
            try:
                owned = await asyncio.to_thread(self._leases.renew, scope, token)
            except Exception:
                _LOGGER.exception("User ingest heartbeat failed for %s", scope)
                continue
            if not owned:
                lost.set()
                worker.cancel()
                return

    def _history_authorization(
        self, scope: UserHistoryScope
    ) -> AuthorizationContext | None:
        """Revalidate one loader scope against active project membership."""
        bindings = OwnerRepository().list_workspace_roles(
            scope.tenant_id,
            scope.workspace_id,
            scope.user_id,
        )
        admitted = any(
            row["instance_status"] == "ready"
            and (
                (
                    row["scope_type"] == "workspace"
                    and row["role"] in {"member", "ws_admin"}
                )
                or (row["scope_type"] == "tenant" and row["role"] == "tenant_admin")
            )
            for row in bindings
        )
        if not admitted:
            return None
        return AuthorizationContext(
            user_id=scope.user_id,
            tenant_id=scope.tenant_id,
            role="member",
            active_workspace_role="member",
            workspace_ids=frozenset({scope.workspace_id}),
            active_workspace_id=scope.workspace_id,
        )


__all__ = ["IngestAdapter"]
