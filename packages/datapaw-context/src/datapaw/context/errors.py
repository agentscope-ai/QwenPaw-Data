"""Stable application error taxonomy shared by HTTP and worker boundaries."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(eq=False)
class DataPawError(Exception):
    code: str
    message: str
    http_status: int = 500
    retryable: bool = False
    details: dict[str, Any] = field(default_factory=dict)

    def __str__(self) -> str:
        return self.message


class ResourceBudgetExceeded(DataPawError):
    def __init__(
        self,
        resource: str,
        *,
        limit: int | float,
        requested: int | float | None = None,
        http_status: int = 429,
    ) -> None:
        details: dict[str, Any] = {"resource": resource, "limit": limit}
        if requested is not None:
            details["requested"] = requested
        super().__init__(
            code="RESOURCE_BUDGET_EXCEEDED",
            message=f"{resource} budget exceeded",
            http_status=http_status,
            retryable=True,
            details=details,
        )


class UpstreamServiceError(DataPawError):
    def __init__(self, service: str, message: str, *, retryable: bool = True) -> None:
        super().__init__(
            code="UPSTREAM_SERVICE_ERROR",
            message=message,
            http_status=502,
            retryable=retryable,
            details={"service": service},
        )


class StateConflictError(DataPawError):
    def __init__(self, message: str, **details: Any) -> None:
        super().__init__(
            code="STATE_CONFLICT",
            message=message,
            http_status=409,
            retryable=False,
            details=details,
        )


def install_error_handlers(app: Any) -> None:
    """Map typed application errors to a stable, retry-aware JSON envelope."""
    from fastapi import Request
    from fastapi.responses import JSONResponse

    async def _handle_datapaw_error(
        _request: Request,
        error: DataPawError,
    ) -> JSONResponse:
        headers = {"Retry-After": "1"} if error.retryable else None
        return JSONResponse(
            status_code=error.http_status,
            headers=headers,
            content={
                "error": {
                    "code": error.code,
                    "message": error.message,
                    "retryable": error.retryable,
                    "details": error.details,
                }
            },
        )

    app.add_exception_handler(DataPawError, _handle_datapaw_error)
