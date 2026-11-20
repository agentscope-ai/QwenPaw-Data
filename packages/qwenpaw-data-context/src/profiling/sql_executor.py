"""将 profiling 的查询接入 Context Manager 的只读 SQL 入口。"""

from __future__ import annotations

from context_manager.api.dependencies import get_context_authorization
from context_manager.sql import SqlResult
from context_manager.schema import Zone
from semantic_config.cm_runtime import current_runtime


def execute_read(
    sql: str, *, datasource_id: str = "", max_rows: int = 200
) -> SqlResult:
    runtime = current_runtime()
    scope = runtime.issue_graph_read_scope(
        get_context_authorization(),
        datasource_id=datasource_id,
        zone=Zone.METADATA.value,
    )
    return runtime.sql_query(scope).execute(
        datasource_id,
        sql,
        max_rows=max_rows,
        timeout_ms=30_000,
        scope=scope,
    )
