"""Own and cache runtime resources at the Workspace boundary."""

from __future__ import annotations

import logging
from collections.abc import Callable
from concurrent.futures import Future
from contextlib import AsyncExitStack
from functools import partial
from pathlib import Path
from threading import Lock

from qwenpaw_data_auth.authorization import AuthorizationContext
from sqlalchemy.engine import Engine

from ..auth import GLOBAL_OWNER_ID, GraphScopeAuthority
from ..compatibility.explorer.cypher import CypherService
from ..compatibility.explorer.raw_cypher import RawCypherService
from ..compatibility.explorer.service import ExplorerService
from ..compatibility.explorer.topics import TopicJobs
from ..executor import BoundedExecutor
from ..knowledge import KnowledgeImportLimits, KnowledgeImportService
from ..metadata import MetadataImportService, MetadataSyncService
from ..metadata.feedback import MetadataFeedbackService
from ..metadata.physical import PhysicalMappingService
from ..metadata.weaving import MetadataWeavingService
from ..metadata.weaving.repository import WeavingRepository
from ..model import ModelConfigRepository, ModelService, OpenAIEmbedCaller
from ..model.usage import ModelUsageEvent
from ..schema import Glossary
from ..settings import get_settings
from ..sql import RdbConnector, SqlQueryService
from ..store import GraphStore
from ..store.runtime import ExecutionTarget
from ..transaction.recovery import RecoveryService
from ..transaction.recovery.stack import RecoveryStack
from .connection import GraphConnectionConfig
from .errors import ContextRuntimeUnavailable

logger = logging.getLogger(__name__)


class WorkspaceRuntime:
    """Own all resources whose lifecycle matches one Workspace graph instance."""

    def __init__(
        self,
        target: ExecutionTarget,
        store: GraphStore,
        rdb_connector: RdbConnector,
        glossary: Glossary,
        *,
        database_engine: Engine,
        executor: BoundedExecutor,
        graph_read_executor: BoundedExecutor,
        model_service_factory: Callable[[str], ModelService] | None = None,
        enqueue_usage: Callable[[ModelUsageEvent], bool] | None = None,
        knowledge_limits: KnowledgeImportLimits | None = None,
        qwenpaw_data_home: Path | None = None,
        explorer_cypher: CypherService | None = None,
        raw_cypher: RawCypherService | None = None,
    ) -> None:
        """Compose Workspace services without exposing this object to FastAPI."""
        if not isinstance(target, ExecutionTarget):
            raise TypeError("target must implement ExecutionTarget")
        if not isinstance(store, GraphStore):
            raise TypeError("store must implement GraphStore")
        if not isinstance(store.access, GraphScopeAuthority):
            raise TypeError("store access must be a GraphScopeAuthority")
        if type(rdb_connector) is not RdbConnector:
            raise TypeError("rdb_connector must be an RdbConnector")
        if type(glossary) is not Glossary:
            raise TypeError("glossary must be a Glossary")
        if glossary.schema is not store.schema:
            raise ValueError("Glossary must govern the runtime Store schema")
        if (
            knowledge_limits is not None
            and type(knowledge_limits) is not KnowledgeImportLimits
        ):
            raise TypeError("knowledge_limits must be KnowledgeImportLimits or None")

        self._target = target
        self._store = store
        self._rdb_connector = rdb_connector
        self._glossary = glossary
        self._models_factory = model_service_factory
        self._enqueue_usage = enqueue_usage
        self._embed_factory = partial(OpenAIEmbedCaller, run_sync=executor.run_sync)
        self._metadata_sync = MetadataSyncService(
            store, database_engine=database_engine
        )
        self.physical_mapping = PhysicalMappingService(
            store, rdb_connector, run_sync=executor.run
        )
        self._weaving_repository = WeavingRepository(database_engine)
        self.explorer = ExplorerService(store, executor=graph_read_executor)
        self.explorer_cypher = explorer_cypher
        self.raw_cypher = raw_cypher
        self.explorer_topics = TopicJobs(self.explorer)
        self._sql_query = SqlQueryService(store.access, rdb_connector)
        self._state_lock = Lock()
        self._closed = False
        self.knowledge_importer = KnowledgeImportService(
            store,
            glossary,
            self.models,
            executor=executor,
            database_engine=database_engine,
            home=qwenpaw_data_home
            if qwenpaw_data_home is not None
            else get_settings().paths.home,
            tenant_id=target.tenant_id,
            workspace_id=target.workspace_id,
            limits=knowledge_limits,
        )

    @property
    def target(self) -> ExecutionTarget:
        """Return the immutable tenant/Workspace graph target."""
        return self._target

    @property
    def store(self) -> GraphStore:
        """Return the Workspace-owned graph Store."""
        return self._store

    @property
    def access(self) -> GraphScopeAuthority:
        """Return the Store-owned CM Auth authority."""
        return self._store.access

    @property
    def rdb_connector(self) -> RdbConnector:
        """Return the Workspace relational connector."""
        return self._rdb_connector

    @property
    def sql_query(self) -> SqlQueryService:
        """Return the Workspace bounded datasource SQL query service."""
        return self._sql_query

    def recovery_stack(self, rid: str) -> RecoveryStack:
        """Bind recovery to this resolved target, independently of request identity."""
        system = AuthorizationContext(
            user_id="__system__",
            tenant_id=self.target.tenant_id,
            role="ws_admin",
            active_workspace_role="ws_admin",
            workspace_ids=frozenset({self.target.workspace_id}),
            admin_workspace_ids=frozenset({self.target.workspace_id}),
            active_workspace_id=self.target.workspace_id,
        )
        return RecoveryStack(
            self._store,
            rid,
            control_scope=self.access.issue_admin_scope(
                system, owner_id=GLOBAL_OWNER_ID, zone="_internal"
            ),
            read_scope=self.access.issue_admin_scope(system),
            scope_for=lambda owner, datasource: self.access.issue_admin_scope(
                system, owner_id=owner, datasource_id=datasource
            ),
        )

    def metadata_importer(
        self,
        user_id: str,
        *,
        executor: BoundedExecutor,
        recovery: RecoveryService | None = None,
    ) -> MetadataImportService:
        """Compose a lightweight importer using shared graph resources."""
        return MetadataImportService(
            self._store,
            self.models(user_id),
            executor=executor,
            recovery=recovery,
        )

    def metadata_weaving(
        self,
        *,
        executor: BoundedExecutor,
        recovery: RecoveryService,
    ) -> MetadataWeavingService:
        """Compose a publisher with the process Runtime's execution policy."""
        return MetadataWeavingService(
            self._store,
            self.models,
            partial(self.metadata_importer, executor=executor),
            self._weaving_repository,
            physical_mapping=self.physical_mapping,
            executor=executor,
            narrow_write_scope=self.access.narrow_write_scope,
            recovery=recovery,
        )

    def metadata_feedback(
        self,
        user_id: str,
        *,
        executor: BoundedExecutor,
    ) -> MetadataFeedbackService:
        """Compose feedback using this Workspace's shared Store and user models."""
        return MetadataFeedbackService(
            self._store,
            self.models(user_id),
            executor=executor,
        )

    @property
    def glossary(self) -> Glossary:
        """Return the Glossary shared by this Workspace runtime."""
        return self._glossary

    @property
    def metadata_sync(self) -> MetadataSyncService:
        """Share the stateless userspace service; each operation supplies its user Scope."""
        return self._metadata_sync

    def models(self, user_id: str) -> ModelService:
        """Provide user configuration and operation-scoped clients."""
        with self._state_lock:
            if self._closed:
                raise ContextRuntimeUnavailable("workspace runtime is closed")
        if self._models_factory is not None:
            return self._models_factory(user_id)
        return ModelService(
            ModelConfigRepository(
                self.target.tenant_id,
                user_id,
                workspace_id=self.target.workspace_id,
            ),
            enqueue_usage=self._enqueue_usage,
            embed_factory=self._embed_factory,
        )

    @property
    def closed(self) -> bool:
        """Return whether Workspace resources were closed."""
        with self._state_lock:
            return self._closed

    def health(self) -> dict[str, object]:
        """Return the Workspace Store health without exposing the runtime."""
        return dict(self._store.health())

    def close(self) -> None:
        """Close synchronous Workspace resources exactly once."""
        with self._state_lock:
            if self._closed:
                return
            self._closed = True
        self._store.close()

    async def stop_services(self) -> None:
        """Stop owned services before execution pools and Store are released."""
        async with AsyncExitStack() as stack:
            stack.push_async_callback(self.knowledge_importer.aclose)
            stack.push_async_callback(self.explorer_topics.aclose)

    async def aclose(self) -> None:
        """Finish Workspace work before releasing the shared Store."""
        async with AsyncExitStack() as stack:
            stack.callback(self.close)
            stack.push_async_callback(self.stop_services)


WorkspaceRuntimeFactory = Callable[[GraphConnectionConfig], WorkspaceRuntime]


class WorkspaceRuntimePool:
    """Own lazily constructed Workspace runtimes and their terminal shutdown."""

    def __init__(self, factory: WorkspaceRuntimeFactory) -> None:
        """Configure Workspace construction without selecting a default target."""
        if not callable(factory):
            raise TypeError("factory must be callable")
        self._factory = factory
        self._runtimes: dict[tuple[str, str], WorkspaceRuntime] = {}
        self._initializing: dict[tuple[str, str], Future[WorkspaceRuntime]] = {}
        self._lock = Lock()
        self._closed = False

    def resolve(self, connection: GraphConnectionConfig) -> WorkspaceRuntime:
        """Return a healthy cached runtime for the exact resolved connection."""
        if not isinstance(connection, GraphConnectionConfig):
            raise TypeError("connection must be a GraphConnectionConfig")
        key = (connection.tenant_id, connection.workspace_id)
        with self._lock:
            if self._closed:
                raise ContextRuntimeUnavailable("workspace runtime pool is closed")
            cached = self._runtimes.get(key)
            if cached is not None and not cached.closed:
                return cached
            pending = self._initializing.get(key)
            creator = pending is None
            if creator:
                pending = Future()
                self._initializing[key] = pending
        if not creator:
            return pending.result()

        runtime: WorkspaceRuntime | None = None
        phase = "create"
        try:
            created = self._factory(connection)
            if type(created) is not WorkspaceRuntime:
                raise TypeError("factory must return a WorkspaceRuntime")
            runtime = created
            if runtime.target != connection.target:
                raise ValueError("WorkspaceRuntime target does not match connection")
            phase = "health"
            if runtime.health().get("ok") is not True:
                raise ContextRuntimeUnavailable(
                    "workspace graph runtime is unavailable"
                )
            phase = "publish"
            with self._lock:
                if self._closed:
                    raise ContextRuntimeUnavailable("workspace runtime pool is closed")
                self._runtimes[key] = runtime
                del self._initializing[key]
                pending.set_result(runtime)
        except BaseException as exc:
            # Complete the shared attempt even when construction is interrupted.
            logger.exception(
                "Workspace runtime initialization failed tenant=%s workspace=%s phase=%s",
                *key,
                phase,
            )
            if isinstance(exc, Exception) and not isinstance(
                exc, ContextRuntimeUnavailable
            ):
                error = ContextRuntimeUnavailable(
                    "workspace graph runtime is unavailable"
                )
                error.__cause__ = exc
            else:
                error = exc
            try:
                if runtime is not None:
                    runtime.close()
            except Exception:
                logger.exception(
                    "Workspace runtime cleanup failed tenant=%s workspace=%s",
                    *key,
                )
            finally:
                with self._lock:
                    del self._initializing[key]
                    pending.set_exception(error)
            raise error
        return runtime

    def close(self) -> None:
        """Close all synchronous Workspace resources exactly once."""
        with self._lock:
            if self._closed:
                return
            self._closed = True
            runtimes = tuple(self._runtimes.values())
            self._runtimes.clear()
        for runtime in runtimes:
            runtime.close()

    async def stop_services(self) -> None:
        """Stop owned services without releasing their Workspace resources."""
        with self._lock:
            runtimes = tuple(self._runtimes.values())
        async with AsyncExitStack() as stack:
            for runtime in reversed(runtimes):
                stack.push_async_callback(runtime.stop_services)

    async def aclose(self) -> None:
        """Close all Workspace resources exactly once."""
        with self._lock:
            if self._closed:
                return
            self._closed = True
            runtimes = tuple(self._runtimes.values())
            self._runtimes.clear()
        async with AsyncExitStack() as stack:
            for runtime in reversed(runtimes):
                stack.push_async_callback(runtime.aclose)


__all__ = [
    "WorkspaceRuntime",
    "WorkspaceRuntimeFactory",
    "WorkspaceRuntimePool",
]
