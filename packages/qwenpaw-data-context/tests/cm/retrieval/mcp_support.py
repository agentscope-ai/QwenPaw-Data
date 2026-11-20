"""Assemble authenticated MCP applications and scoped in-memory graph fixtures."""

from __future__ import annotations

from types import SimpleNamespace

from context_manager.api.app import bind_context_runtime, create_api_app
from context_manager.api.authentication import install_context_authentication
from context_manager.schema import GRAPH_SCHEMA
from context_manager.sql import RdbConnector
from qwenpaw_data_auth.config import AuthConfig
from fastapi import HTTPException, Request

from cm.infrastructure.access.support import authorization
from cm.infrastructure.lifecycle.support import bound_context_runtime
from cm.infrastructure.storage.schema_support import make_glossary
from cm.knowledge.support import MemoryGraphStore

ACCEPT = {"Accept": "application/json, text/event-stream"}


def build_app(*, authenticated=True, rdb_connector=None, model_config_dsn=None):
    store = MemoryGraphStore()
    runtime = bound_context_runtime(
        store,
        rdb_connector if rdb_connector is not None else RdbConnector(),
        make_glossary(GRAPH_SCHEMA),
        model_config_dsn=model_config_dsn,
    )
    store._runtime = SimpleNamespace(target=runtime.fixed_target)
    app = create_api_app()
    bind_context_runtime(app, runtime)
    authorization_calls = []

    def authorizer(request: Request):
        token = request.headers.get("authorization")
        authorization_calls.append(token)
        if token in {"Bearer user-1", "Bearer user-2"}:
            return authorization(user_id=token.split()[-1])
        if token == "Bearer other-workspace":
            return authorization(workspace_id="workspace-2")
        raise HTTPException(status_code=401, detail="Not authenticated")

    if authenticated:
        install_context_authentication(
            app,
            AuthConfig(
                secret_keys=["test-secret"], control_plane_dsn="sqlite:///:memory:"
            ),
            runtime.fixed_target,
            authorizer=authorizer,
        )
    return app, store, runtime, authorization_calls


def rpc(method, *, params=None, request_id=1):
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "method": method,
        "params": params or {},
    }


def call_tool(client, name, arguments=None, *, token="user-1", **headers):
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return client.post(
        "/mcp/v1/cm",
        headers={"Accept": "application/json, text/event-stream", **headers},
        json=rpc("tools/call", params={"name": name, "arguments": arguments or {}}),
    )
