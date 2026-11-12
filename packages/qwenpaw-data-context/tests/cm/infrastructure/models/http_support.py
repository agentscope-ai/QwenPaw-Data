"""Build API fixtures backed by synthetic Host Providers and model choices."""

from unittest.mock import Mock

from context_manager.api.app import bind_context_runtime, create_api_app
from context_manager.api.dependencies import require_context_authorization
from context_manager.model import service
from context_manager.runtime import Neo4jConnectionConfig, create_context_runtime
from context_manager.runtime import context as runtime_context
from fastapi.testclient import TestClient

from cm.infrastructure.access.support import authorization
from cm.infrastructure.models.support import (
    FakeCaller,
    configure_models,
    create_host_db,
)


def build_config_client(tmp_path, monkeypatch, *, configured=True, role="member"):
    _dsn, _, _, _ = create_host_db(tmp_path, monkeypatch)
    if configured:
        configure_models(("tenant-1", "user-1"))
    monkeypatch.setattr(service, "OpenAILlmCaller", FakeCaller)
    monkeypatch.setattr(runtime_context, "OpenAIEmbedCaller", FakeCaller)
    application = create_api_app()
    driver = Mock(side_effect=ConnectionError("graph is offline"))
    runtime = create_context_runtime(
        Neo4jConnectionConfig(
            tenant_id="tenant-1",
            workspace_id="workspace-1",
            neo4j_uri="bolt://offline.invalid:7687",
            neo4j_user="neo4j",
            neo4j_password="test",
        ),
        driver_factory=driver,
    )
    bind_context_runtime(application, runtime)
    application.state.graph_driver = driver
    application.dependency_overrides[require_context_authorization] = lambda: (
        authorization(role=role)
    )
    return TestClient(application)
