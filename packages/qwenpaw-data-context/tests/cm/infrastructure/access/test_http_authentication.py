"""Verify every mounted CM compatibility surface enters authentication."""

from __future__ import annotations

from context_manager.api.app import bind_context_runtime, create_api_app
from context_manager.api.authentication import install_context_authentication
from context_manager.schema import GRAPH_SCHEMA
from context_manager.sql import RdbConnector
from context_manager.store.neo4j import GraphTarget
from qwenpaw_data_auth.authorization import AuthorizationContext
from qwenpaw_data_auth.config import AuthConfig
from fastapi import HTTPException, Request
from fastapi.testclient import TestClient

from cm.infrastructure.lifecycle.support import bound_context_runtime
from cm.infrastructure.storage.schema_support import make_glossary
from cm.knowledge.support import MemoryGraphStore


def test_native_and_legacy_routes_are_authentication_managed() -> None:
    """Reject requests before dependencies across maintained and legacy paths."""
    runtime = bound_context_runtime(
        MemoryGraphStore(),
        RdbConnector(),
        make_glossary(GRAPH_SCHEMA),
    )
    application = create_api_app()
    bind_context_runtime(application, runtime)
    attempted_paths: list[str] = []

    def reject(request: Request) -> AuthorizationContext:
        attempted_paths.append(request.url.path)
        raise HTTPException(status_code=401, detail="Not authenticated")

    install_context_authentication(
        application,
        AuthConfig(
            secret_keys=["test-secret"],
            control_plane_dsn="sqlite:///:memory:",
        ),
        GraphTarget("tenant-1", "workspace-1"),
        authorizer=reject,
    )

    requests = [
        ("GET", "/api/v1/ug/users/user-1"),
        ("POST", "/api/cm/user/ingest"),
        ("PUT", "/api/v1/admin/kg/entities/ent:customer"),
        ("POST", "/api/v1/rdb/execute_sql"),
        ("GET", "/api/v1/config"),
        ("DELETE", "/api/v1/config"),
        ("PUT", "/api/v1/config/llm"),
        ("POST", "/api/v1/config/llm/test"),
        ("PUT", "/api/v1/config/embed"),
        ("POST", "/api/v1/config/embed/test"),
        ("POST", "/api/v1/embed/embed-all"),
        ("GET", "/api/v1/embed/jobs/latest"),
        ("GET", "/api/v1/embed/jobs/job-1"),
        ("POST", "/api/v1/embed/jobs/job-1/retry"),
        ("GET", "/api/system/model-config/"),
        ("POST", "/api/system/model-config/embedding/rebuild"),
    ]
    with TestClient(application) as client:
        for method, path in requests:
            response = client.request(method, path)
            assert response.status_code == 401, (method, path, response.text)
            assert response.json() == {"detail": "Not authenticated"}

    assert attempted_paths == [path for _, path in requests]
