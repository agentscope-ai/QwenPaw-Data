"""阻塞型依赖不得从 async endpoint 直接执行。"""

from __future__ import annotations

import inspect
import json

from qwenpaw_data.context.blocking_io import (
    BlockingIOOverloaded,
    BlockingIOTimeout,
    BlockingPool,
)

from context_manager.api import (
    cm_api,
    ctx_service,
    doc_api,
    executor,
    import_api,
    semantic_api,
    semantic_io_api,
    server,
    tg_admin_api,
    trace_api,
)
from semantic_config.routers import weave_task_router


def test_openpyxl_and_trace_routes_run_in_fastapi_threadpool():
    blocking_endpoints = [
        semantic_io_api.export_semantic,
        semantic_io_api.import_semantic,
        semantic_io_api.import_preview,
        semantic_io_api.import_confirm,
        semantic_io_api.import_from_excel_convert,
        semantic_io_api.import_from_excel_apply,
        trace_api.ingest_trace_events,
        trace_api.ingest_trace_file,
        trace_api.list_traces,
        trace_api.get_trace_graph,
        trace_api.download_trace_file,
        trace_api.submit_trace,
    ]

    assert all(not inspect.iscoroutinefunction(endpoint) for endpoint in blocking_endpoints)


def test_sync_endpoint_delegation_is_not_awaited_directly():
    # ``trace_api.get_trace_graph`` is a sync endpoint. Calling it with await
    # bypasses FastAPI's worker boundary and raises TypeError after doing the
    # blocking graph query on the event loop.
    assert not inspect.iscoroutinefunction(tg_admin_api.get_task_graph)


def test_async_routes_use_the_governor_instead_of_default_to_thread():
    assert "asyncio.to_thread" not in inspect.getsource(doc_api)
    assert "run_in_executor" not in inspect.getsource(doc_api)
    assert "asyncio.to_thread" not in inspect.getsource(import_api)

    endpoints = {
        route.path: route.endpoint
        for route in server.app.routes
        if hasattr(route, "endpoint")
    }
    for path in ("/api/domains", "/api/resolve", "/api/expand"):
        endpoint = endpoints[path]
        assert inspect.iscoroutinefunction(endpoint)
        assert "_run_blocking" in inspect.getsource(endpoint)

    assert not inspect.iscoroutinefunction(endpoints["/api/admin/reset_memory"])


def test_context_and_sql_async_paths_use_bounded_resource_pools():
    for endpoint in (
        cm_api.cm_search_context,
        cm_api.cm_explore_entity,
        cm_api.cm_execute_sql,
        cm_api.cm_execute_sql_stream,
        cm_api.cm_recall_experience,
    ):
        source = inspect.getsource(endpoint)
        assert "blocking_io" in source

    assert "asyncio.to_thread" not in inspect.getsource(cm_api)
    assert not inspect.iscoroutinefunction(ctx_service.zoom_entity)
    assert not inspect.iscoroutinefunction(ctx_service.recall_experience)
    assert not inspect.iscoroutinefunction(ctx_service.record_outcome)

    sql_source = inspect.getsource(executor.execute_sql_async)
    assert "BlockingPool.SQL" in sql_source
    assert "governor.run" in sql_source


def test_semantic_import_http_paths_forward_the_app_governor():
    assert "governor=req.app.state.blocking_io" in inspect.getsource(
        semantic_api.semantic_import
    )
    assert "governor=governor" in inspect.getsource(
        weave_task_router.submit_task
    )


def test_background_writeback_uses_governed_submission():
    endpoints = {
        route.path: route.endpoint
        for route in server.app.routes
        if hasattr(route, "endpoint")
    }
    for path in ("/api/execute_sql", "/api/feedback"):
        source = inspect.getsource(endpoints[path])
        assert "blocking_io.submit" in source
        assert "async_mode=False" in source


async def test_blocking_failures_have_explicit_http_overload_semantics():
    overload_handler = server.app.exception_handlers[BlockingIOOverloaded]
    overload_response = await overload_handler(
        None,
        BlockingIOOverloaded(BlockingPool.GRAPH, "graph.read", "saturated"),
    )
    assert overload_response.status_code == 503
    assert overload_response.headers["retry-after"] == "1"
    assert json.loads(overload_response.body)["pool"] == "graph"

    timeout_handler = server.app.exception_handlers[BlockingIOTimeout]
    timeout_response = await timeout_handler(
        None,
        BlockingIOTimeout(BlockingPool.SQL, "sql.execute", "expired"),
    )
    assert timeout_response.status_code == 504
    assert json.loads(timeout_response.body)["pool"] == "sql"
