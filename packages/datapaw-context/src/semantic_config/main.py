from __future__ import annotations

from contextlib import asynccontextmanager
import os

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from semantic_config.config import get_settings
from semantic_config.db import init_db
from semantic_config.errors import register_exception_handlers
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


@asynccontextmanager
async def lifespan(_: FastAPI):
    await init_db()
    yield


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(title="数据语义配置 - 本地版", version="0.1.0", lifespan=lifespan)

    # Keep the standalone semantic-config process behind the same auth boundary
    # as the unified server. Install before CORS so error responses retain CORS.
    from context_manager.api.auth import install_api_token_auth
    from context_manager.api.security import configured_cors_origins

    cors_origins = configured_cors_origins(
        os.environ.get("DATAPAW_CORS_ORIGINS") or settings.cors_origins
    )
    install_api_token_auth(
        app,
        enforce_scopes=True,
        allowed_origins=cors_origins,
    )

    # CORS：显式来源白名单；路径前缀由各路由统一 /api。
    app.add_middleware(
        CORSMiddleware,
        allow_origins=cors_origins,
        allow_credentials=False,
        allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
        allow_headers=["Accept", "Authorization", "Content-Type", "X-Request-ID"],
        expose_headers=[
            "RateLimit-Limit",
            "RateLimit-Remaining",
            "Retry-After",
            "X-Request-ID",
        ],
    )

    register_exception_handlers(app)

    app.include_router(datasource_router.router)
    app.include_router(datasource_router.metadata_router)
    app.include_router(biz_domain_router.router)
    app.include_router(dataset_meta_router.router)
    app.include_router(dataset_column_meta_router.router)
    app.include_router(dimension_router.router)
    app.include_router(dataset_dimension_router.router)
    app.include_router(metric_lib_router.router)
    app.include_router(metric_formula_lib_router.router)
    app.include_router(import_router.router)
    app.include_router(weave_task_router.router)

    @app.get("/health")
    async def health():
        return {"status": "ok"}

    return app


app = create_app()
