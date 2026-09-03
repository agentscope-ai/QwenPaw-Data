# -*- coding: utf-8 -*-
from __future__ import annotations

from typing import NoReturn

from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse

from qwenpaw_data.host.core.api.models.common import ErrorBodySchema
from qwenpaw_data.host.core.domain.steer import SteerChatEndedError


def raise_api(
    code: str,
    message: str,
    *,
    status: int,
    details: dict | None = None,
) -> NoReturn:
    raise HTTPException(
        status_code=status,
        detail=ErrorBodySchema(
            code=code,  # type: ignore[arg-type]
            message=message,
            details=details,
        ).model_dump(),
    )


def map_domain_error(exc: Exception) -> HTTPException | None:
    reason = getattr(exc, "reason", None)
    details = {"reason": reason} if isinstance(reason, str) else None
    if isinstance(exc, SteerChatEndedError):
        return HTTPException(
            status_code=409,
            detail=ErrorBodySchema(
                code="CONFLICT",
                message=str(exc) or "active chat has ended",
                details={"reason": "CHAT_ENDED"},
            ).model_dump(),
        )
    if isinstance(exc, PermissionError):
        return HTTPException(
            status_code=403,
            detail=ErrorBodySchema(code="FORBIDDEN", message=str(exc)).model_dump(),
        )
    if isinstance(exc, LookupError):
        return HTTPException(
            status_code=404,
            detail=ErrorBodySchema(
                code="NOT_FOUND",
                message=str(exc),
                details=details,
            ).model_dump(),
        )
    if isinstance(exc, RuntimeError) and str(exc).startswith("CONFLICT"):
        return HTTPException(
            status_code=409,
            detail=ErrorBodySchema(
                code="CONFLICT",
                message=str(exc),
                details=details,
            ).model_dump(),
        )
    if isinstance(exc, ValueError):
        return HTTPException(
            status_code=400,
            detail=ErrorBodySchema(code="VALIDATION", message=str(exc)).model_dump(),
        )
    if isinstance(exc, NotImplementedError):
        return HTTPException(
            status_code=501,
            detail=ErrorBodySchema(code="VALIDATION", message=str(exc)).model_dump(),
        )
    return None


async def http_exception_handler(
    _request: Request,
    exc: HTTPException,
) -> JSONResponse:
    detail = exc.detail
    if isinstance(detail, dict) and "code" in detail:
        return JSONResponse(status_code=exc.status_code, content=detail)
    return JSONResponse(
        status_code=exc.status_code,
        content=ErrorBodySchema(code="VALIDATION", message=str(detail)).model_dump(),
    )
