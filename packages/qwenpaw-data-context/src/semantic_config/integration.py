"""把 semantic-config（编辑层）挂进 CM 的单一 FastAPI app。

设计见 docs/MERGE_DESIGN.md：
- 单进程、单端口，两套路由共存（编辑 `/api/xxx` 与 CM `/api/v1/*`）。
- 异常按路径分派：编辑路由沿用 semantic-config 的错误协议，其余回退 CM 原有处理，
  两侧前端返回协议均不变。
- 复用 CM 的统一认证与 scope 授权中间件。
"""
from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.exception_handlers import request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response

from semantic_config.errors import (
    BadRequestError,
    NotFoundError,
    NotFoundNoBody,
    _body,
)
from semantic_config.routers import (
    biz_domain_router,
    dataset_column_meta_router,
    dataset_dimension_router,
    dataset_meta_router,
    datasource_router,
    dimension_router,
    import_router,
    metric_formula_lib_router,
    metric_lib_router,
    weave_task_router,
)

# 编辑层统一挂在 /api/semantic-config 下做隔离（各 router 自身前缀已是 /api/semantic-config/xxx，
# 避免与 CM dashboard 的 /api/* 兜底路由相撞）。命中该前缀的请求走 semantic-config 错误协议，其余回退 CM。
SC_MOUNT_PREFIX = "/api/semantic-config"
EDITING_PREFIXES: tuple[str, ...] = (SC_MOUNT_PREFIX,)


def _is_editing_path(request: Request) -> bool:
    path = request.url.path
    return any(path.startswith(p) for p in EDITING_PREFIXES)


def _register_exception_handlers(app: FastAPI) -> None:
    """按路径分派的异常处理：编辑侧用 semantic-config 协议，CM 侧回退原逻辑。"""

    # 编辑侧独有异常类型：只会由编辑路由抛出，无条件套用编辑协议。
    @app.exception_handler(BadRequestError)
    async def _bad_request(_: Request, exc: BadRequestError):
        return JSONResponse(status_code=400, content=_body(400, exc.message))

    @app.exception_handler(NotFoundError)
    async def _not_found(_: Request, exc: NotFoundError):
        return JSONResponse(status_code=404, content=_body(404, exc.message))

    @app.exception_handler(NotFoundNoBody)
    async def _not_found_no_body(_: Request, __: NotFoundNoBody):
        return Response(status_code=404)

    # 校验错误：编辑路由 -> 400 + 编辑协议；其余 -> FastAPI 默认（422），与 CM 现状一致。
    @app.exception_handler(RequestValidationError)
    async def _validation(request: Request, exc: RequestValidationError):
        if not _is_editing_path(request):
            return await request_validation_exception_handler(request, exc)
        errors = exc.errors()
        msg = "参数校验失败"
        if errors:
            loc = ".".join(str(x) for x in errors[0].get("loc", []) if x != "body")
            detail = errors[0].get("msg", "")
            msg = f"{loc}: {detail}".strip(": ") or detail
        return JSONResponse(status_code=400, content=_body(400, msg))

    # 兜底异常：编辑路由 -> 500 + 编辑协议；其余 -> 回退到 CM 之前注册的兜底 handler。
    prev_exc_handler = app.exception_handlers.get(Exception)

    @app.exception_handler(Exception)
    async def _fallback(request: Request, exc: Exception):
        if _is_editing_path(request):
            return JSONResponse(
                status_code=500, content=_body(500, "服务器内部错误，请联系管理员")
            )
        if prev_exc_handler is not None:
            return await prev_exc_handler(request, exc)
        return JSONResponse(status_code=500, content={"detail": "Internal Server Error"})


def mount_semantic_config(app: FastAPI) -> None:
    """在 CM `create_app()` 末尾调用：挂载编辑层路由 + 注册按路径分派的异常处理器。

    须在 CM 自身的 include_router / exception_handler 都注册完之后调用，
    以便正确捕获并回退 CM 既有的兜底异常处理逻辑。
    """
    # 各 router 自身前缀已是 /api/semantic-config/xxx，直接挂载即可。
    for r in (
        datasource_router,
        biz_domain_router,
        dataset_meta_router,
        dataset_column_meta_router,
        dimension_router,
        dataset_dimension_router,
        metric_lib_router,
        metric_formula_lib_router,
        import_router,
        weave_task_router,
    ):
        app.include_router(r.router)
    app.include_router(datasource_router.metadata_router)

    _register_exception_handlers(app)
