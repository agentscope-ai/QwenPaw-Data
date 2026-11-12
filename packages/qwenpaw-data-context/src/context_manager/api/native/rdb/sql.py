"""Expose controlled relational SQL HTTP models and handler."""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from ....sql import (
    DatasourceError,
    DatasourceErrorCode,
    SqlAdmissionError,
    SqlExecutionError,
)
from ....schema import Zone
from ...dependencies import (
    AuthorizationDependency,
    RuntimeDependency,
)


class ExecuteSqlRequest(BaseModel):
    """Submit one bounded read-only query for an authorized datasource."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    datasource_id: Annotated[
        str,
        StringConstraints(
            strip_whitespace=True,
            min_length=1,
            max_length=128,
        ),
    ]
    sql: str = Field(min_length=1, max_length=50_000)
    max_rows: int = Field(default=200, ge=1, le=1000)
    timeout_ms: int = Field(default=10_000, ge=100, le=30_000)
    odps_execution_project: str | None = Field(default=None, min_length=1)


class ExecuteSqlResponse(BaseModel):
    """Return a detached bounded SQL result without query or driver details."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    columns: list[str]
    rows: list[list[Any]]
    row_count: int = Field(ge=0)
    truncated: bool
    elapsed_ms: float = Field(ge=0)


router = APIRouter()


@router.post("/execute_sql", response_model=ExecuteSqlResponse)
async def execute_sql(
    request: ExecuteSqlRequest,
    runtime: RuntimeDependency,
    context: AuthorizationDependency,
) -> ExecuteSqlResponse:
    """Execute one admitted bounded query through the application runtime."""

    def execute():
        scope = runtime.issue_graph_read_scope(
            context,
            datasource_id=request.datasource_id,
            zone=Zone.METADATA.value,
        )
        result = runtime.sql_query(scope).execute(
            request.datasource_id,
            request.sql,
            max_rows=request.max_rows,
            timeout_ms=request.timeout_ms,
            scope=scope,
            odps_execution_project=request.odps_execution_project,
        )
        return ExecuteSqlResponse(
            columns=list(result.columns),
            rows=[list(row) for row in result.rows],
            row_count=result.row_count,
            truncated=result.truncated,
            elapsed_ms=result.elapsed_ms,
        )

    try:
        return await runtime.run_sql(execute)
    except BlockingIOError as exc:
        raise HTTPException(503, "SQL execution is busy") from exc
    except PermissionError as exc:
        raise HTTPException(403, "datasource SQL execution is unavailable") from exc
    except SqlAdmissionError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=str(exc),
        ) from exc
    except DatasourceError as exc:
        if exc.code is DatasourceErrorCode.SCOPE_MISMATCH:
            response_status = status.HTTP_403_FORBIDDEN
        elif exc.code is DatasourceErrorCode.NOT_FOUND:
            response_status = status.HTTP_404_NOT_FOUND
        elif exc.code is DatasourceErrorCode.INVALID_RECORD:
            response_status = status.HTTP_500_INTERNAL_SERVER_ERROR
        else:
            response_status = status.HTTP_503_SERVICE_UNAVAILABLE
        raise HTTPException(
            status_code=response_status,
            detail="datasource SQL execution is unavailable",
        ) from exc
    except SqlExecutionError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="datasource SQL execution failed",
        ) from exc


__all__ = ["ExecuteSqlRequest", "ExecuteSqlResponse", "execute_sql", "router"]
