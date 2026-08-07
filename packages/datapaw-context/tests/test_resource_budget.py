from __future__ import annotations

import asyncio

import httpx
import pytest
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route

from datapaw.context.errors import ResourceBudgetExceeded
from datapaw.context.errors import install_error_handlers
from datapaw.context.resource_budget import (
    RequestBudget,
    ResourceBudgetMiddleware,
    ResourceLimits,
)


def _limits(**overrides):
    values = {
        "request_timeout_seconds": 1.0,
        "max_upload_bytes": 1024,
        "max_response_bytes": 2048,
        "max_sql_rows": 100,
        "max_cypher_rows": 50,
        "max_graph_nodes": 20,
        "max_graph_edges": 40,
        "max_llm_tokens": 1000,
        "max_llm_retries": 2,
        "max_external_concurrency": 4,
    }
    values.update(overrides)
    return ResourceLimits(**values)


def test_budget_caps_query_and_graph_results() -> None:
    budget = RequestBudget(_limits())
    assert budget.cap_sql_rows(1000) == 100
    assert budget.cap_cypher_rows(1000) == 50
    assert budget.cap_graph(nodes=500, edges=500) == (20, 40)


def test_budget_rejects_excess_llm_tokens_and_attempts() -> None:
    budget = RequestBudget(_limits())
    budget.reserve_llm(tokens=900, attempts=2)
    with pytest.raises(ResourceBudgetExceeded):
        budget.reserve_llm(tokens=101)
    with pytest.raises(ResourceBudgetExceeded):
        budget.reserve_llm(tokens=1, attempts=2)


def test_budget_rejects_oversized_materialized_response() -> None:
    budget = RequestBudget(_limits(max_response_bytes=10))
    with pytest.raises(ResourceBudgetExceeded):
        budget.ensure_response_payload({"payload": "too large"})


@pytest.mark.asyncio
async def test_request_timeout_returns_typed_504(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DATAPAW_REQUEST_TIMEOUT_SECONDS", "0.01")

    async def slow(_request):
        await asyncio.sleep(0.05)
        return JSONResponse({"ok": True})

    app = Starlette(routes=[Route("/slow", slow)])
    app.add_middleware(ResourceBudgetMiddleware)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/slow")
    assert response.status_code == 504
    assert response.json()["error"]["code"] == "RESOURCE_BUDGET_EXCEEDED"


@pytest.mark.asyncio
async def test_typed_errors_have_stable_http_envelope() -> None:
    async def limited(_request):
        raise ResourceBudgetExceeded("rows", limit=10, requested=11)

    app = Starlette(routes=[Route("/limited", limited)])
    install_error_handlers(app)
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/limited")
    assert response.status_code == 429
    assert response.headers["retry-after"] == "1"
    assert response.json() == {
        "error": {
            "code": "RESOURCE_BUDGET_EXCEEDED",
            "message": "rows budget exceeded",
            "retryable": True,
            "details": {"resource": "rows", "limit": 10, "requested": 11},
        }
    }
