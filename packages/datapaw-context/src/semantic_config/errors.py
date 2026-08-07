from __future__ import annotations

from datetime import datetime, timezone
from http import HTTPStatus

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response


class BadRequestError(Exception):
    """参数校验失败 / 同名冲突 / 删除时存在引用等 -> 400。"""

    def __init__(self, message: str):
        self.message = message
        super().__init__(message)


class NotFoundError(Exception):
    """资源不存在且需带 message body -> 404（如 PUT/DELETE 的目标不存在）。"""

    def __init__(self, message: str):
        self.message = message
        super().__init__(message)


class NotFoundNoBody(Exception):
    """GET/{id} 查询不存在 -> 404 且无 body（对齐 ResponseEntity.notFound()）。"""


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _body(status: int, message: str) -> dict:
    return {
        "timestamp": _utc_now(),
        "status": status,
        "error": HTTPStatus(status).phrase,
        "message": message,
    }


def register_exception_handlers(app: FastAPI) -> None:
    @app.exception_handler(BadRequestError)
    async def _bad_request(_: Request, exc: BadRequestError):
        return JSONResponse(status_code=400, content=_body(400, exc.message))

    @app.exception_handler(NotFoundError)
    async def _not_found(_: Request, exc: NotFoundError):
        return JSONResponse(status_code=404, content=_body(404, exc.message))

    @app.exception_handler(NotFoundNoBody)
    async def _not_found_no_body(_: Request, __: NotFoundNoBody):
        return Response(status_code=404)

    @app.exception_handler(RequestValidationError)
    async def _validation(_: Request, exc: RequestValidationError):
        # 请求体/参数不合法，归一为 400，取第一条错误信息
        errors = exc.errors()
        msg = "参数校验失败"
        if errors:
            loc = ".".join(str(x) for x in errors[0].get("loc", []) if x != "body")
            detail = errors[0].get("msg", "")
            msg = f"{loc}: {detail}".strip(": ") or detail
        return JSONResponse(status_code=400, content=_body(400, msg))

    @app.exception_handler(Exception)
    async def _fallback(_: Request, __: Exception):
        return JSONResponse(status_code=500, content=_body(500, "服务器内部错误，请联系管理员"))
