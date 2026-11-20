"""Compose bound runtime graphs for tests without physical infrastructure."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from context_manager.db import get_database, initialize_database
from context_manager.model import ModelService
from context_manager.runtime import (
    ContextRuntime,
    Neo4jConnectionConfig,
    WorkspaceResolver,
    WorkspaceRuntime,
    WorkspaceRuntimePool,
)
from context_manager.runtime.execution import ExecutionResources
from context_manager.schema import Glossary
from context_manager.schema.relational import external_tables
from context_manager.settings.db import DbSettings
from context_manager.settings.execution import ExecutionSettings
from context_manager.sql import RdbConnector
from context_manager.store import GraphStore
from sqlalchemy import Column, MetaData, Table, Text
from sqlalchemy.engine import Engine

from cm.infrastructure.storage.schema_support import relational_database
from cm.metadata.support import UnstackedImports


def create_external_tables(engine: Engine, tables=external_tables) -> None:
    """Seed empty external dependencies for service-lifecycle tests."""
    metadata = MetaData()
    for projection in tables:
        Table(
            projection.name,
            metadata,
            *(Column(name, Text) for name in projection.columns.keys()),  # noqa: SIM118 - ColumnCollection iterates columns, not names
        )
    metadata.create_all(engine)


def bound_context_runtime(
    store: GraphStore,
    rdb_connector: RdbConnector,
    glossary: Glossary,
    *,
    models: ModelService | None = None,
    model_service_factory: Callable[[str], ModelService] | None = None,
    model_config_dsn: str | None = None,
    tenant_id: str = "tenant-1",
    workspace_id: str = "workspace-1",
    qwenpaw_data_home: Path | None = None,
    database_engine: Engine | None = None,
) -> ContextRuntime:
    """Wrap one explicit WorkspaceRuntime behind the process ContextRuntime."""
    try:
        database = get_database()
    except RuntimeError:
        database = initialize_database(
            DbSettings(url=model_config_dsn) if model_config_dsn is not None else None
        )
        if model_config_dsn is None:
            create_external_tables(database.engine)
    settings = Neo4jConnectionConfig(
        tenant_id=tenant_id,
        workspace_id=workspace_id,
        neo4j_uri="bolt://test.invalid",
        neo4j_user="neo4j",
        neo4j_password="test-password",
    )
    execution = ExecutionResources(ExecutionSettings())
    workspace = WorkspaceRuntime(
        settings.target,
        store,
        rdb_connector,
        glossary,
        executor=execution.background,
        graph_read_executor=execution.graph_read,
        database_engine=(
            database_engine if database_engine is not None else relational_database()
        ),
        model_service_factory=(
            (lambda user: models)
            if models is not None
            else model_service_factory
            if model_service_factory is not None
            else None
        ),
        qwenpaw_data_home=qwenpaw_data_home,
    )
    # These component fixtures exercise plans/routing without starting recovery.
    # Started Runtime and recovery integration tests use the real service.
    metadata_importer = workspace.metadata_importer

    def unstacked_importer(user_id, *, executor, recovery=None):
        return metadata_importer(
            user_id,
            executor=executor,
            recovery=recovery if recovery is not None else UnstackedImports(store),
        )

    workspace.metadata_importer = unstacked_importer
    pool = WorkspaceRuntimePool(lambda _settings: workspace)
    runtime = ContextRuntime(
        WorkspaceResolver(pool, connection=settings), execution=execution
    )
    if models is not None or model_service_factory is not None:
        runtime.models = lambda context: workspace.models(context.user_id)
    pool.resolve(settings)
    metadata_weaving = runtime.metadata_weaving

    def unstacked_weaving(scope):
        if runtime._recovery is not None:
            return metadata_weaving(scope)
        return runtime._workspace_for_scope(scope).metadata_weaving(
            executor=execution.background,
            recovery=UnstackedImports(store),
        )

    runtime.metadata_weaving = unstacked_weaving
    return runtime


__all__ = ["bound_context_runtime"]
