"""Host payloads use the native Metadata endpoint and trusted personal identity."""

from context_manager.api import (
    bind_context_runtime,
    create_api_app,
    install_context_authentication,
)
from context_manager.schema import GRAPH_SCHEMA, METRIC
from context_manager.sql import RdbConnector
from fastapi import HTTPException
from fastapi.testclient import TestClient

from cm.infrastructure.access.support import authorization
from cm.infrastructure.lifecycle.support import bound_context_runtime
from cm.infrastructure.lifecycle.test_startup import _auth_config
from cm.infrastructure.storage.schema_support import make_glossary
from cm.metadata.test_feedback import card, feedback_graph  # noqa: F401


def test_host_preview_confirm_duplicate_and_authentication(feedback_graph):  # noqa: F811 - shared fixture
    store, own, _, models = feedback_graph
    runtime = bound_context_runtime(
        store, RdbConnector(), make_glossary(GRAPH_SCHEMA), models=models
    )
    app = create_api_app()
    bind_context_runtime(app, runtime)

    def authorize(request):
        if not request.headers.get("Authorization"):
            raise HTTPException(401, "Not authenticated")
        return authorization("alice")

    install_context_authentication(
        app, _auth_config(), runtime.fixed_target, authorizer=authorize
    )
    try:
        with TestClient(app) as client:
            url = "/api/v1/semantic/feedback_card"
            payload = card().model_dump(mode="json")
            assert client.post(url, json=payload).status_code == 401
            client.headers["Authorization"] = "Bearer alice"
            response = client.post(url, json=payload)
            assert response.status_code == 200, response.text
            assert response.json()["mode"] == "test"
            assert not store.list_nodes(METRIC, scope=own)
            response = client.post(url + "?mode=confirm", json=payload)
            assert response.status_code == 200, response.text
            assert response.json()["status"] == "accepted"
            assert store.get_nodes(METRIC, tuple(response.json()["keys"]), scope=own)[0]
            assert (
                client.post(url + "?mode=confirm", json=payload).json()["status"]
                == "duplicate"
            )
            assert (
                client.post(url, json={**payload, "owner_id": "__global__"}).status_code
                == 422
            )
            assert (
                client.post(url, json={**payload, "domain": "unknown"}).json()[
                    "detail"
                ]["code"]
                == "INVALID_DOMAIN"
            )
    finally:
        runtime.close()
