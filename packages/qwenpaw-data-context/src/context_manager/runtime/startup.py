"""Construct Context and Workspace runtimes from resolved connections."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from functools import partial
from typing import Any

from neo4j import GraphDatabase

from ..auth import GraphScopeAuthority
from ..compatibility.explorer.cypher import CypherService
from ..compatibility.explorer.raw_cypher import RawCypherService
from ..db import CMDatabase, close_database, get_database, initialize_database
from ..executor import BoundedExecutor
from ..knowledge import DocumentLimits, KnowledgeImportLimits
from ..model import ModelService
from ..model.repository import ModelUsageRepository
from ..model.usage import ModelUsageEvent
from ..model.usage_recorder import ModelUsageRecorder
from ..schema import GRAPH_SCHEMA, Glossary
from ..settings import get_settings
from ..settings.cron import CronSettings
from ..sql import RdbConnector
from ..store.neo4j import Neo4jGraphStore, Neo4jRuntime
from ..user.loader import ChatLoader
from .connection import (
    GraphConnectionConfig,
    HoloConnectionConfig,
    Neo4jConnectionConfig,
)
from .context import ContextRuntime, RuntimeInitializer
from .execution import ExecutionResources
from .resolver import WorkspaceResolver
from .workspace import (
    WorkspaceRuntime,
    WorkspaceRuntimePool,
)

DriverFactory = Callable[..., Any]
DependencyInitializer = Callable[[], Awaitable[None]]


async def initialize_semantic_stores() -> None:
    """Initialize editing and profiling tables through their owning modules."""
    from profiling.db import init_probe_db
    from semantic_config.db import init_db

    await init_db()
    await init_probe_db()


def create_service_runtime(
    connection: GraphConnectionConfig | None = None,
    *,
    driver_factory: DriverFactory | None = None,
    semantic_initializer: DependencyInitializer | None = initialize_semantic_stores,
    cron_settings: CronSettings | None = None,
    history_source: str = "sql",
    history_loader: ChatLoader | None = None,
) -> ContextRuntime:
    """Compose the process lifecycle while deferring database I/O to startup."""
    if history_source not in {"sql", "remote"}:
        raise ValueError("history_source must be sql or remote")
    if history_source == "remote" and history_loader is None:
        raise ValueError("remote history_source requires an explicit history_loader")

    async def initialize_stores() -> CMDatabase:
        database = await asyncio.to_thread(initialize_database)
        try:
            if semantic_initializer is not None:
                await semantic_initializer()
            await asyncio.to_thread(
                database.validate_dependencies, history_source=history_source
            )
            return database
        except BaseException:
            close_database()
            raise

    return create_context_runtime(
        connection,
        driver_factory=driver_factory,
        initializer=initialize_stores,
        cron_settings=cron_settings,
        history_loader=history_loader,
    )


def create_workspace_runtime(
    connection: GraphConnectionConfig,
    *,
    executor: BoundedExecutor,
    graph_read_executor: BoundedExecutor,
    driver_factory: DriverFactory | None = None,
    model_service_factory: Callable[[str], ModelService] | None = None,
    enqueue_usage: Callable[[ModelUsageEvent], bool] | None = None,
) -> WorkspaceRuntime:
    """Create one Workspace Store and its directly owned resources."""
    if not isinstance(connection, (Neo4jConnectionConfig, HoloConnectionConfig)):
        raise TypeError("unsupported graph connection configuration")
    deployment = get_settings()
    engine = get_database().engine
    glossary = Glossary(
        GRAPH_SCHEMA,
        engine,
        tenant_id=connection.tenant_id,
        workspace_id=connection.workspace_id,
    )
    driver = None
    graph_runtime = None
    store = None
    try:
        access = GraphScopeAuthority()
        raw_cypher = None
        explorer_cypher = None
        if isinstance(connection, HoloConnectionConfig):
            from ..store.holoage import (
                HoloDriver,
                HoloGraphStore,
                HoloRuntime,
                build_physical_schema,
            )

            target = connection.target
            factory = driver_factory or HoloDriver
            driver = factory(
                connect_timeout=15,
                max_size=deployment.graph.holo_pool_max_size,
                timeout=deployment.graph.holo_pool_timeout,
                max_idle=deployment.graph.holo_pool_max_idle,
                **deployment.graph.connection_params(),
            )
            graph_runtime = HoloRuntime(driver, target)
            store = HoloGraphStore(
                graph_runtime,
                GRAPH_SCHEMA,
                access,
                glossary,
                catalog=build_physical_schema(
                    embed_dim=deployment.embedding.dimensions or 1024
                ),
            )
        elif isinstance(connection, Neo4jConnectionConfig):
            target = connection.target
            factory = driver_factory or GraphDatabase.driver
            driver = factory(
                connection.neo4j_uri,
                auth=(connection.neo4j_user, connection.neo4j_password),
                notifications_min_severity="OFF",
            )
            graph_runtime = Neo4jRuntime(driver, target)
            store = Neo4jGraphStore(graph_runtime, GRAPH_SCHEMA, access, glossary)
            raw_cypher = RawCypherService(driver, target)
            explorer_cypher = CypherService(driver, target)
        knowledge_settings = deployment.knowledge
        return WorkspaceRuntime(
            target,
            store,
            RdbConnector(),
            glossary,
            database_engine=engine,
            executor=executor,
            graph_read_executor=graph_read_executor,
            raw_cypher=raw_cypher,
            explorer_cypher=explorer_cypher,
            qwenpaw_data_home=deployment.paths.home,
            model_service_factory=model_service_factory,
            enqueue_usage=enqueue_usage,
            knowledge_limits=KnowledgeImportLimits(
                document=DocumentLimits(
                    max_bytes=knowledge_settings.max_upload_bytes,
                    max_pages=knowledge_settings.max_document_pages,
                    max_chars=knowledge_settings.max_document_chars,
                ),
                max_concurrency=knowledge_settings.max_concurrency,
                embedding_dimensions=deployment.embedding.dimensions,
            ),
        )
    except BaseException:
        if store is not None:
            store.close()
        elif graph_runtime is not None:
            graph_runtime.close()
        elif driver is not None:
            driver.close()
        raise


def create_context_runtime(
    connection: GraphConnectionConfig | None = None,
    *,
    driver_factory: DriverFactory | None = None,
    initializer: RuntimeInitializer | None = None,
    cron_settings: CronSettings | None = None,
    history_loader: ChatLoader | None = None,
) -> ContextRuntime:
    """Compose one resolver and pool; omit connection to use the Workspace registry."""
    execution = ExecutionResources(get_settings().execution)
    usage_recorder = ModelUsageRecorder(ModelUsageRepository())
    pool = WorkspaceRuntimePool(
        partial(
            create_workspace_runtime,
            driver_factory=driver_factory,
            executor=execution.background,
            graph_read_executor=execution.graph_read,
            enqueue_usage=usage_recorder.enqueue,
        )
    )
    return ContextRuntime(
        WorkspaceResolver(pool, connection=connection),
        initializer=initializer,
        cron_settings=cron_settings,
        execution=execution,
        usage_recorder=usage_recorder,
        history_loader=history_loader,
    )


__all__ = [
    "DriverFactory",
    "create_context_runtime",
    "create_service_runtime",
    "create_workspace_runtime",
]
