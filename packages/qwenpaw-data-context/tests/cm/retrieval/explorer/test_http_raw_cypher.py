"""Frontend URL, grant header and HTTP error envelopes reach compatibility."""

from context_manager.api import (
    bind_context_runtime,
    create_api_app,
    install_context_authentication,
)
from context_manager.runtime import ContextRuntime
from context_manager.schema import GRAPH_SCHEMA
from context_manager.sql import RdbConnector
from fastapi import HTTPException
from fastapi.testclient import TestClient

from cm.infrastructure.lifecycle.support import bound_context_runtime
from cm.infrastructure.lifecycle.test_startup import _auth_config
from cm.infrastructure.storage.schema_support import make_glossary
from cm.knowledge.support import MemoryGraphStore
from cm.retrieval.explorer.test_raw_cypher import WRITE, raw  # noqa: F401


def test_frontend_raw_contract_and_request_auth(raw, monkeypatch):  # noqa: F811 - shared fixture
    service, _, request, dsn = raw
    args = request()
    runtime = bound_context_runtime(
        MemoryGraphStore(),
        RdbConnector(),
        make_glossary(GRAPH_SCHEMA),
        tenant_id="t1",
        workspace_id="w1",
    )
    monkeypatch.setattr(ContextRuntime, "raw_cypher", lambda self, context: service)
    app = create_api_app()
    bind_context_runtime(app, runtime)

    def authorize(request):
        if not request.headers.get("Authorization"):
            raise HTTPException(401, "Not authenticated")
        return args["context"]

    install_context_authentication(
        app, _auth_config(dsn), runtime.fixed_target, authorizer=authorize
    )
    try:
        with TestClient(app) as client:
            url = "/api/v1/admin/cypher/raw"
            payload = {"cypher": WRITE, "params": {}, "limit": 10}
            assert client.post(url, json=payload).status_code == 401
            client.headers["Authorization"] = "Bearer admin"
            missing = client.post(url, json=payload)
            assert missing.status_code == 403
            assert missing.json()["detail"]["error"]["code"] == "BREAK_GLASS_REQUIRED"
            client.headers["X-QwenPaw-Data-Breakglass-Token"] = args["token"]
            response = client.post(url, json=payload)
            assert response.status_code == 200, response.text
            assert response.json()["ok"] is True
            assert response.json()["data"]["rows"] == [{"value": 1}]
            assert client.post(url, json=payload).status_code == 403
    finally:
        runtime.close()
