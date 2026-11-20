"""Expose one process-level runtime facade over Workspace selection."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from contextlib import AsyncExitStack, ExitStack
from functools import partial
from threading import Lock

from qwenpaw_data_auth.authorization import AuthorizationContext

from ..auth import (
    GraphScopeAuthority,
    GraphScopeError,
    ScopeBinding,
    WriteOwner,
    active_target,
    require_target,
)
from ..compatibility.explorer.cypher import CypherService
from ..compatibility.explorer.raw_cypher import RawCypherService
from ..compatibility.explorer.service import ExplorerService
from ..compatibility.explorer.topics import TopicJobs
from ..db import CMDatabase, close_database, get_database
from ..embedding.service import EmbeddingRebuildService, resume_expired_rebuilds
from ..executor import drain
from ..knowledge import (
    DocumentQueryService,
    KnowledgeDeleteService,
    KnowledgeImportService,
)
from ..metadata import MetadataImportService, MetadataSyncService
from ..metadata.feedback import MetadataFeedbackService
from ..metadata.physical import PhysicalMappingService
from ..metadata.weaving import MetadataWeavingService
from ..model import ModelConfigRepository, ModelService, OpenAIEmbedCaller
from ..model.usage_recorder import ModelUsageRecorder
from ..schema import Glossary
from ..settings.cron import CronSettings
from ..sql import RdbConnector, SqlQueryService
from ..store import GraphStore
from ..store.runtime import ExecutionTarget
from ..transaction.recovery import RecoveryService
from ..transaction.recovery.repository import RecoveryRunRepository
from ..user.crud import MemoryCrudService
from ..user.loader import ChatLoader
from .execution import ExecutionResources
from .resolver import WorkspaceResolver
from .workspace import WorkspaceRuntime

_LOGGER = logging.getLogger(__name__)

RuntimeInitializer = Callable[[], Awaitable[CMDatabase]]


class ContextRuntime:
    """Own process services, Workspace selection and dependency-ordered shutdown."""

    def __init__(
        self,
        resolver: WorkspaceResolver,
        *,
        execution: ExecutionResources,
        initializer: RuntimeInitializer | None = None,
        cron_settings: CronSettings | None = None,
        usage_recorder: ModelUsageRecorder | None = None,
        history_loader: ChatLoader | None = None,
    ) -> None:
        """Bind Workspace resolution and its resource lifecycle."""
        from ..cron import CronManager
        from ..embedding import EmbeddingJobManager
        from ..user import IngestAdapter

        self._resolver = resolver
        self._initializer = initializer
        self._database: CMDatabase | None = None
        self._recovery: RecoveryService | None = None
        self._state_lock = Lock()
        self._closed = False
        self._shutdown_task = None
        self._execution = execution
        self._usage_recorder = usage_recorder
        self.user_ingest = IngestAdapter(
            lambda context: self._workspace_for_authorization(context).store,
            lambda context: self._workspace_for_authorization(context).models(
                context.user_id
            ),
            executor=self._execution.background,
            loader=history_loader,
        )
        self.cron = CronManager(self.user_ingest, settings=cron_settings)
        self.embedding_jobs = EmbeddingJobManager()
        self._sweep_stop: asyncio.Event | None = None
        self._sweep_task: asyncio.Task | None = None

    async def start(self) -> None:
        """Initialize process storage and start scheduled work."""
        try:
            if self._initializer is not None:
                initialization = asyncio.create_task(self._initializer())
                try:
                    self._database = await asyncio.shield(initialization)
                except asyncio.CancelledError:
                    # Initialization may still own a SQL worker; drain it before cleanup.
                    self._database = await initialization
                    raise
            if self._usage_recorder is not None:
                self._usage_recorder.start()
            if self._recovery is None:
                self._recovery = RecoveryService(
                    RecoveryRunRepository(get_database().engine),
                    self._execution.background,
                    lambda identity: self._workspace_target(
                        identity["tenant_id"], identity["workspace_id"]
                    ).recovery_stack(identity["rid"]),
                )
            self._recovery.start()
            await self.cron.start()
            if self._database is not None:
                await resume_expired_rebuilds(
                    self.embedding_jobs, self.embedding_rebuild
                )
            from context_manager.heartbeat import run_orphan_sweep

            self._sweep_stop = asyncio.Event()
            self._sweep_task = asyncio.create_task(run_orphan_sweep(self._sweep_stop))
        except BaseException:
            await self.aclose()
            raise

    @property
    def recovery(self) -> RecoveryService:
        """Return the process recovery service after Runtime startup."""
        if self._recovery is None:
            raise RuntimeError("runtime recovery has not started")
        return self._recovery

    @property
    def fixed_target(self) -> ExecutionTarget | None:
        """Return the only accepted target for fixed mode, otherwise None."""
        return self._resolver.fixed_target

    @property
    def closed(self) -> bool:
        """Return whether application runtime shutdown has completed."""
        with self._state_lock:
            return self._closed

    def health(self) -> dict[str, object]:
        """Report whether the configured Workspace source is available."""
        return {"ok": not self.closed and self._resolver.health()}

    async def run_graph_read(self, operation, /, *args, **kwargs):
        """Run a foreground query step with the Runtime's read budget."""
        return await self._execution.graph_read.run(operation, *args, **kwargs)

    async def run_background(self, operation, /, *args, **kwargs):
        """Run a background business step with the Runtime's background budget."""
        return await self._execution.background.run(operation, *args, **kwargs)

    async def run_sql(self, operation, /, *args, **kwargs):
        """Run a user SQL request with the Runtime's SQL budget."""
        return await self._execution.sql.run(operation, *args, **kwargs)

    def user_crud(self, scope: ScopeBinding) -> MemoryCrudService:
        """Compose explicit user edits with the authorized Workspace Store."""
        return MemoryCrudService(self.graph_store(scope))

    def graph_store(self, scope: ScopeBinding) -> GraphStore:
        """Return the Store selected and authorized by an issued Scope."""
        return self._workspace_for_scope(scope).store

    def explorer(self, scope: ScopeBinding) -> ExplorerService:
        """Select the authorized Workspace's Graph Center reader."""
        return self._workspace_for_scope(scope).explorer

    def raw_cypher(self, context: AuthorizationContext) -> RawCypherService:
        """Select the compatibility executor using the authorized Workspace."""
        from .errors import ContextRuntimeUnavailable

        service = self._workspace_for_authorization(context).raw_cypher
        if service is None:
            raise ContextRuntimeUnavailable("raw cypher service is unavailable")
        return service

    def explorer_cypher(self, context: AuthorizationContext) -> CypherService:
        """Select the authenticated Workspace's scoped Cypher application."""
        from .errors import ContextRuntimeUnavailable

        service = self._workspace_for_authorization(context).explorer_cypher
        if service is None:
            raise ContextRuntimeUnavailable("Explorer Cypher runtime is unavailable")
        return service

    def explorer_topics(self, scope: ScopeBinding) -> TopicJobs:
        """Select workspace-owned topic tasks; results remain owner-scoped."""
        return self._workspace_for_scope(scope).explorer_topics

    def graph_scope_authority(self, scope: ScopeBinding) -> GraphScopeAuthority:
        """Return the authority for the issued Scope's Workspace."""
        return self._workspace_for_scope(scope).access

    def issue_graph_read_scope(
        self,
        context: AuthorizationContext,
        *,
        datasource_id: str | None = None,
        zone: str | None = None,
        owner_id: str | None = None,
        review: bool = False,
    ) -> ScopeBinding:
        """Issue a read scope from the selected Workspace authority."""
        return self._workspace_for_authorization(context).access.issue_read_scope(
            context,
            datasource_id=datasource_id,
            zone=zone,
            owner_id=owner_id,
            review=review,
        )

    def issue_graph_write_scope(
        self,
        context: AuthorizationContext,
        *,
        owner: WriteOwner,
        datasource_id: str | None = None,
        zone: str | None = None,
    ) -> ScopeBinding:
        """Issue a write capability from the selected Workspace authority."""
        return self._workspace_for_authorization(context).access.issue_write_scope(
            context,
            owner=owner,
            datasource_id=datasource_id,
            zone=zone,
        )

    def narrow_read_scope(
        self,
        scope: ScopeBinding,
        *,
        datasource_id: str | None,
        zone: str | None,
    ) -> ScopeBinding:
        """Route one valid capability to its authority and narrow it."""
        return self.graph_scope_authority(scope).narrow_read_scope(
            scope,
            datasource_id=datasource_id,
            zone=zone,
        )

    def rdb_connector(self, scope: ScopeBinding) -> RdbConnector:
        """Return the selected Workspace relational connector."""
        return self._workspace_for_scope(scope).rdb_connector

    def sql_query(self, scope: ScopeBinding) -> SqlQueryService:
        """Return the selected Workspace datasource SQL query service."""
        return self._workspace_for_scope(scope).sql_query

    def metadata_importer(self, scope: ScopeBinding) -> MetadataImportService:
        """Return the selected Workspace metadata import service."""
        return self._workspace_for_scope(scope).metadata_importer(
            scope.user_id,
            executor=self._execution.background,
            recovery=self._recovery,
        )

    def metadata_feedback(self, scope: ScopeBinding) -> MetadataFeedbackService:
        """Select native feedback for the issued personal write capability."""
        return self._workspace_for_scope(scope).metadata_feedback(
            scope.user_id,
            executor=self._execution.background,
        )

    async def metadata_sync_status(self, context: AuthorizationContext):
        """Run Workspace/Scope preparation and sync-status reading in the query pool."""

        def read():
            scope = self.issue_graph_read_scope(context, owner_id=context.user_id)
            return self.metadata_sync(scope).status(scope)

        return await self._execution.graph_read.run(read)

    async def metadata_sync_pull(self, context: AuthorizationContext):
        """Run a complete personal Metadata pull in the background pool."""

        def sync():
            scope = self.issue_graph_write_scope(context, owner=WriteOwner.USER)
            return scope, self.metadata_sync(scope).sync(scope)

        return await self._execution.background.run(sync)

    async def metadata_sync_resolve(
        self, context: AuthorizationContext, global_digest, resolutions
    ):
        """Apply conflict decisions in the same pool as Metadata pulls."""

        def resolve():
            scope = self.issue_graph_write_scope(context, owner=WriteOwner.USER)
            return scope, self.metadata_sync(scope).resolve(
                global_digest, resolutions, scope
            )

        return await self._execution.background.run(resolve)

    def metadata_sync(self, scope: ScopeBinding) -> MetadataSyncService:
        """Return the workspace service for private Metadata pull and append operations."""
        return self._workspace_for_scope(scope).metadata_sync

    def metadata_weaving(self, scope: ScopeBinding) -> MetadataWeavingService:
        """Select the workspace publisher; all writes consume an issued authoritative scope."""
        return self._workspace_for_scope(scope).metadata_weaving(
            executor=self._execution.background,
            recovery=self.recovery,
        )

    def physical_mapping(self, scope: ScopeBinding) -> PhysicalMappingService:
        """Select source-to-graph physical mapping for the authorized Workspace."""
        return self._workspace_for_scope(scope).physical_mapping

    def glossary(self, scope: ScopeBinding) -> Glossary:
        """Return the Glossary owned by the selected Workspace runtime."""
        return self._workspace_for_scope(scope).glossary

    def knowledge_importer(
        self, context: AuthorizationContext
    ) -> KnowledgeImportService:
        """Select the shared Workspace importer for a trusted identity."""
        return self._workspace_for_authorization(context).knowledge_importer

    def knowledge_documents(
        self, context: AuthorizationContext
    ) -> DocumentQueryService:
        """Compose this user's document reads with the foreground budget."""
        importer = self.knowledge_importer(context)
        return DocumentQueryService(
            importer.document_repository(context),
            importer.document_files(context),
            run_sync=self._execution.graph_read.run,
        )

    def knowledge_deleter(
        self, context: AuthorizationContext
    ) -> KnowledgeDeleteService:
        """Compose this user's deletion with the Workspace document coordination."""
        return self.knowledge_importer(context).deleter(context)

    async def embedding_rebuild(
        self, context: AuthorizationContext
    ) -> EmbeddingRebuildService:
        """Compose a rebuild service with the selected Workspace and background budget."""
        workspace = await self._execution.background.run(
            self._workspace_for_authorization, context
        )

        def scopes_for():
            scope = workspace.store.access.issue_admin_scope(context)
            return scope, lambda owner: workspace.store.access.issue_admin_scope(
                context, owner_id=owner
            )

        return EmbeddingRebuildService(
            workspace.store,
            workspace.models,
            self.embedding_jobs,
            executor=self._execution.background,
            recovery=self.recovery,
            scopes_for=scopes_for,
        )

    def models(
        self,
        context: AuthorizationContext,
    ) -> ModelService:
        """Bind SQL-backed model access without resolving graph resources."""
        if self.closed:
            raise RuntimeError("context runtime is closed")
        tenant_id, workspace_id = active_target(context)
        require_target(context, tenant_id=tenant_id, workspace_id=workspace_id)
        self._resolver.require_workspace(tenant_id, workspace_id)
        return ModelService(
            ModelConfigRepository(
                tenant_id, context.user_id, workspace_id=workspace_id
            ),
            enqueue_usage=(
                self._usage_recorder.enqueue
                if self._usage_recorder is not None
                else None
            ),
            embed_factory=partial(
                OpenAIEmbedCaller, run_sync=self._execution.graph_read.run
            ),
        )

    def close(self) -> None:
        """Close synchronous Workspace resources exactly once."""
        with self._state_lock:
            if self._closed:
                return
            if self._recovery is not None:
                raise RuntimeError("use aclose() after Runtime startup")
        try:
            with ExitStack() as stack:
                if self._usage_recorder is not None:
                    stack.callback(self._usage_recorder.stop)
                stack.callback(self._resolver.close)
                stack.callback(self._execution.close)
        finally:
            with self._state_lock:
                self._closed = True

    async def aclose(self) -> None:
        """Finish process jobs before releasing Workspace and SQL resources."""
        with self._state_lock:
            if self._closed:
                return
            if self._shutdown_task is None:
                self._shutdown_task = asyncio.create_task(self._shutdown())
            shutdown = self._shutdown_task
        await drain(shutdown)

    async def _stop_sweep(self) -> None:
        if self._sweep_stop is not None:
            self._sweep_stop.set()
        if self._sweep_task is not None:
            self._sweep_task.cancel()
            await asyncio.gather(self._sweep_task, return_exceptions=True)

    async def _shutdown(self) -> None:
        try:
            async with AsyncExitStack() as stack:
                if self._database is not None:
                    stack.callback(close_database)
                if self._usage_recorder is not None:
                    stack.push_async_callback(
                        asyncio.to_thread, self._usage_recorder.stop
                    )
                stack.callback(self._resolver.close)
                stack.push_async_callback(self._execution.aclose)
                stack.push_async_callback(self._resolver.stop_services)
                if self._recovery is not None:
                    stack.push_async_callback(self._recovery.aclose)
                stack.push_async_callback(self.embedding_jobs.aclose)
                stack.push_async_callback(self.cron.shutdown)
                stack.push_async_callback(self._stop_sweep)
        finally:
            with self._state_lock:
                self._closed = True

    def _workspace_for_authorization(
        self,
        context: AuthorizationContext,
    ) -> WorkspaceRuntime:
        """Select a Workspace only while issuing or checking non-graph access."""
        tenant_id, workspace_id = active_target(context)
        require_target(context, tenant_id=tenant_id, workspace_id=workspace_id)
        return self._workspace_target(tenant_id, workspace_id)

    def _workspace_for_scope(
        self,
        scope: ScopeBinding,
    ) -> WorkspaceRuntime:
        """Route and authenticate an issued Scope before exposing services."""
        if type(scope) is not ScopeBinding:
            raise GraphScopeError("scope must be a concrete ScopeBinding")
        workspace = self._workspace_target(scope.tenant_id, scope.workspace_id)
        workspace.access.require(scope, scope.mode)
        return workspace

    def _workspace_target(
        self,
        tenant_id: str,
        workspace_id: str,
    ) -> WorkspaceRuntime:
        """Resolve one authorized target into its Workspace runtime."""
        if self.closed:
            raise RuntimeError("context runtime is closed")
        return self._resolver.resolve(tenant_id, workspace_id)


__all__ = ["ContextRuntime"]
