"""阻塞型依赖不得从 async endpoint 直接执行。"""

from __future__ import annotations

import inspect
import json

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
from context_manager.metadata.weaving import MetadataWeavingService
from qwenpaw_data.context.blocking_io import (
    BlockingIOOverloaded,
    BlockingIOTimeout,
    BlockingPool,
)
from semantic_config.routers import weave_task_router


def _mounted_endpoints(routes, prefix=""):
    """Flatten Starlette's registered-router wrappers for source assertions."""
    endpoints = {}
    for route in routes:
        included = getattr(route, "original_router", None)
        if included is not None:
            context = getattr(route, "include_context", None)
            child_prefix = prefix + (getattr(context, "prefix", "") or "")
            endpoints.update(_mounted_endpoints(included.routes, child_prefix))
        elif hasattr(route, "endpoint"):
            endpoints[prefix + route.path] = route.endpoint
    return endpoints


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


def test_retrieval_surfaces_stay_on_the_sync_worker_path():
    from context_manager.api import explorer_api, retrieval

    for endpoint in (
        explorer_api.global_graph,
        explorer_api.search_subgraph,
        cm_api.cm_search_event,
    ):
        assert not inspect.iscoroutinefunction(endpoint)

    retrieval_source = inspect.getsource(retrieval)
    assert "asyncio.to_thread" not in retrieval_source
    assert "run_in_executor" not in retrieval_source


def test_async_routes_use_the_governor_instead_of_default_to_thread():
    assert "asyncio.to_thread" not in inspect.getsource(doc_api)
    assert "run_in_executor" not in inspect.getsource(doc_api)
    assert "asyncio.to_thread" not in inspect.getsource(import_api)

    endpoints = _mounted_endpoints(server.app.routes)
    for path in (
        "/api/v1/cm/domains",
        "/api/v3/cm/explore",
        "/api/v1/graph/expand_neighbors",
    ):
        endpoint = endpoints[path]
        assert inspect.iscoroutinefunction(endpoint)
        assert "runtime.run_graph_read" in inspect.getsource(endpoint)


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
    assert "publisher.submit_task" in inspect.getsource(weave_task_router.submit_task)
    assert "executor=self._executor" in inspect.getsource(
        MetadataWeavingService.submit_task
    )


def test_background_writeback_uses_governed_submission():
    endpoints = _mounted_endpoints(server.app.routes)
    sql_source = inspect.getsource(endpoints["/api/v1/rdb/execute_sql"])
    feedback_source = inspect.getsource(
        endpoints["/api/v1/semantic/feedback_card"]
    )
    assert "runtime.run_sql" in sql_source
    assert "runtime.run_background" in feedback_source


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
