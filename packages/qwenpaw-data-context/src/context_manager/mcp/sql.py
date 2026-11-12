"""Expose scoped SQL reads with CSV and separately authorized typed writes."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import asdict
from typing import Annotated

from qwenpaw_data_auth.authorization import AuthorizationContext
from mcp.server.fastmcp import Context, FastMCP
from mcp.server.fastmcp.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import Field, StringConstraints
from pydantic_core import to_json

from ..auth import WriteOwner
from ..runtime import ContextRuntime, ContextRuntimeUnavailable
from ..sql import DatasourceError, SqlAdmissionError, SqlExecutionError

DatasourceId = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1),
    Field(
        description=(
            "Datasource ID in the workspace connection catalog, not a graph node key or display name. "
            "Use datasource_id returned by datasource discovery tools or datasource configuration."
        )
    ),
]
SqlText = Annotated[
    str,
    Field(
        min_length=1,
        max_length=50_000,
        description="One read-only SELECT/WITH query in the selected datasource's SQL dialect; use verified table and column names.",
    ),
]
MaxRows = Annotated[
    int,
    Field(
        strict=True,
        ge=1,
        le=10_000,
        description="Maximum result rows saved in the CSV (1-10000, default 2000). Inline rows preview at most 20; truncated=true means the CSV is also incomplete.",
    ),
]
TimeoutMs = Annotated[
    int,
    Field(
        strict=True,
        ge=100,
        le=30_000,
        description="Statement timeout for PostgreSQL/Hologres, MySQL and SQLite (100-30000 ms). ODPS/BigQuery use SDK default job waiting.",
    ),
]


def register_sql_tools(
    mcp: FastMCP,
    resolve_context: Callable[[Context, str], tuple[ContextRuntime, AuthorizationContext]],
) -> None:
    """Register read/create/modify tools with request-owned services and Scopes."""

    @mcp.tool(
        annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False),
        structured_output=False,
    )
    async def execute_read(
        datasource_id: DatasourceId,
        sql: SqlText,
        ctx: Context,
        max_rows: MaxRows = 2000,
        timeout_ms: TimeoutMs = 10_000,
    ) -> str:
        """Execute one read-only SELECT/WITH query on an explicitly selected datasource.

        Use after graph lookups have established the metric expression, physical
        tables and columns. Connections are read directly from the workspace
        catalog by the datasource_id argument within the authenticated request
        scope; no graph node is required. Supports PostgreSQL,
        Hologres, MySQL, StarRocks, Doris, TiDB, SQLite, DuckDB, BigQuery
        and public ODPS (AccessKey/STS).

        Return {columns, rows, row_count, total_row_count, truncated, elapsed_ms,
        download_url}. rows previews at most 20 rows aligned with columns;
        row_count counts preview rows. total_row_count uses the driver-reported
        count, falling back to the number fetched (at most max_rows + 1), which
        may be a lower bound. The CSV contains at most max_rows data rows.
        truncated=true means even the CSV is incomplete; narrow the query,
        aggregate or use SQL pagination. Empty results have no download URL.

        Host downloads download_url with the same user's authentication and
        replaces it with file_path. Downloads expire after the configured retention
        period (24 hours by default). timeout_ms
        controls PostgreSQL/Hologres, MySQL and SQLite queries; ODPS/BigQuery
        use SDK default job waiting. Writes, DDL and multiple statements are rejected.
        """

        try:
            runtime, authorization = resolve_context(ctx, "execute_read")

            def execute() -> str:
                scope = runtime.issue_graph_read_scope(
                    authorization, owner_id=authorization.user_id
                )
                service = runtime.sql_query(scope)
                result = dict(
                    service.export_csv(
                        datasource_id,
                        sql,
                        max_rows=max_rows,
                        timeout_ms=timeout_ms,
                        scope=scope,
                    )
                )
                result_id = result.pop("result_id")
                url = f"/api/v1/cm/downloads/{result_id}.csv" if result_id else None
                return to_json({**result, "download_url": url}).decode("utf-8")

            return await runtime.run_sql(execute)
        except BlockingIOError as exc:
            raise ToolError("SQL execution is busy") from exc
        except ContextRuntimeUnavailable as exc:
            raise ToolError("Context runtime is unavailable") from exc
        except SqlAdmissionError as exc:
            raise ToolError(str(exc)) from exc
        except PermissionError as exc:
            raise ToolError("SQL read is forbidden") from exc
        except DatasourceError as exc:
            raise ToolError("Datasource is unavailable for SQL execution") from exc
        except SqlExecutionError as exc:
            raise ToolError(str(exc)) from exc

    async def write(
        ctx,
        datasource_id,
        sql,
        statement_type,
        max_rows,
        timeout_ms,
    ):
        try:
            runtime, authorization = resolve_context(ctx, f"execute_{statement_type}")

            def execute():
                scope = runtime.issue_graph_write_scope(
                    authorization, owner=WriteOwner.WORKSPACE, zone="metadata"
                )
                result = runtime.sql_query(scope).execute_write(
                    datasource_id,
                    sql,
                    statement_type=statement_type,
                    max_rows=max_rows,
                    timeout_ms=timeout_ms,
                    scope=scope,
                )
                return to_json(asdict(result)).decode("utf-8")

            return await runtime.run_sql(execute)
        except PermissionError as exc:
            raise ToolError("SQL writes require workspace administration") from exc
        except SqlAdmissionError as exc:
            raise ToolError(str(exc)) from exc
        except DatasourceError as exc:
            raise ToolError("Datasource is unavailable for SQL execution") from exc
        except SqlExecutionError as exc:
            raise ToolError(str(exc)) from exc
        except ContextRuntimeUnavailable as exc:
            raise ToolError("Context runtime is unavailable") from exc
        except BlockingIOError as exc:
            raise ToolError("SQL execution is busy") from exc

    @mcp.tool(
        annotations=ToolAnnotations(
            readOnlyHint=False, destructiveHint=True, idempotentHint=False
        ),
        structured_output=False,
    )
    async def execute_create(
        datasource_id: DatasourceId,
        sql: Annotated[
            str,
            Field(
                min_length=1,
                max_length=50000,
                description="One CREATE TABLE or CREATE VIEW statement, including CREATE TABLE AS SELECT. No CREATE INDEX, OR REPLACE, DROP, ALTER, TRUNCATE or multiple statements.",
            ),
        ],
        ctx: Context,
        max_rows: Annotated[
            int,
            Field(
                strict=True,
                ge=1,
                le=10000,
                description="Maximum inline returned rows, if the CREATE produces rows (1-10000, default 2000). No CSV is produced.",
            ),
        ] = 2000,
        timeout_ms: TimeoutMs = 10000,
    ) -> str:
        """Execute one CREATE TABLE/VIEW or CTAS on an explicit business datasource.

        Use only for a user-requested creation after checking the target with
        execute_read. Requires current Workspace administration and database-side
        write privileges; SQLite also requires a datasource configured read_only=false.
        The statement commits before success is returned. This creates SQL objects,
        not graph nodes. CREATE OR REPLACE and destructive DDL are rejected.
        ODPS CREATE TABLE should specify LIFECYCLE; job success takes effect immediately.

        Return {columns,rows,row_count,truncated,elapsed_ms}; row_count is the
        driver's affected count, or -1 when unknown (common for DDL/ODPS).
        No download_url is returned. Deadlines apply to PostgreSQL/Hologres and
        SQLite; MySQL and ODPS/BigQuery write jobs use driver/SDK waiting behavior.
        After success verify the object with execute_read. If a write outcome is
        uncertain, inspect the target before retrying.
        """
        return await write(
            ctx,
            datasource_id,
            sql,
            "create",
            max_rows,
            timeout_ms,
        )

    @mcp.tool(
        annotations=ToolAnnotations(
            readOnlyHint=False, destructiveHint=True, idempotentHint=False
        ),
        structured_output=False,
    )
    async def execute_modify(
        datasource_id: DatasourceId,
        sql: Annotated[
            str,
            Field(
                min_length=1,
                max_length=50000,
                description="One INSERT or UPDATE statement, including INSERT SELECT, supported upserts and RETURNING. No WITH-prefixed write, DELETE, MERGE, DDL or multiple statements.",
            ),
        ],
        ctx: Context,
        max_rows: Annotated[
            int,
            Field(
                strict=True,
                ge=1,
                le=10000,
                description="Maximum inline RETURNING rows (1-10000, default 2000). The complete statement commits even when returned rows are truncated.",
            ),
        ] = 2000,
        timeout_ms: TimeoutMs = 10000,
    ) -> str:
        """Execute one user-requested INSERT or UPDATE on an explicit business datasource.

        Use execute_read first to verify the target and UPDATE WHERE scope, and
        query again after success. Requires current Workspace administration and
        database write privileges; SQLite requires read_only=false in its config.
        Each statement commits before returning. It changes business table rows,
        not graph properties. SELECT, DELETE, MERGE, DDL, WITH-prefixed writes and
        multiple statements are rejected. ODPS UPDATE requires a transaction table;
        successful ODPS jobs are immediately effective and cannot be undone here.

        Return {columns,rows,row_count,truncated,elapsed_ms}; row_count is affected
        rows, or -1 when the driver does not report it. RETURNING rows are bounded
        by max_rows, but the entire mutation commits. No CSV is produced. Deadlines
        apply to PostgreSQL/Hologres and SQLite; MySQL and ODPS/BigQuery write jobs
        use driver/SDK waiting behavior. On permission denial do not retry. On an
        uncertain outcome verify the target before resubmitting.
        """
        return await write(
            ctx,
            datasource_id,
            sql,
            "modify",
            max_rows,
            timeout_ms,
        )
