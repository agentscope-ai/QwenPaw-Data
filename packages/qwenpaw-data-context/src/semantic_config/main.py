from __future__ import annotations

from contextlib import asynccontextmanager
import os

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from profiling.db import init_probe_db
from profiling.integration import mount_profiling
from semantic_config.config import get_settings
from semantic_config.db import init_db
from semantic_config.engine import reset_engine
from semantic_config.errors import register_exception_handlers
from semantic_config.routers import (
    biz_domain_router,
    dataset_dimension_router,
    dataset_field_router,
    dataset_meta_router,
    datasource_router,
    dimension_router,
    explore_router,
    export_router,
    import_router,
    metric_caliber_router,
    metric_lib_router,
    table_column_router,
    verified_query_router,
    weave_task_router,
)


@asynccontextmanager
async def lifespan(_: FastAPI):
    try:
        await init_db()
        await init_probe_db()
        yield
    finally:
        try:
            from profiling.services.orchestrator import shutdown_probes

            await shutdown_probes()
        finally:
            await reset_engine()


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(title="数据语义配置 - 本地版", version="0.3.0", lifespan=lifespan)

    # Keep the standalone semantic-config process behind the same auth boundary
    # as the unified server. Install before CORS so error responses retain CORS.
    from context_manager.api.auth import install_api_token_auth
    from context_manager.api.security import configured_cors_origins

    cors_origins = configured_cors_origins(
        os.environ.get("QWENPAW_DATA_CORS_ORIGINS") or settings.cors_origins
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
    app.include_router(biz_domain_router.router)
    app.include_router(dataset_meta_router.router)
    app.include_router(table_column_router.router)
    app.include_router(dataset_field_router.router)
    app.include_router(dataset_field_router.legacy_router)
    app.include_router(dimension_router.router)
    app.include_router(dataset_dimension_router.router)
    app.include_router(metric_lib_router.router)
    app.include_router(metric_caliber_router.router)
    app.include_router(metric_caliber_router.legacy_router)
    app.include_router(verified_query_router.router)
    app.include_router(import_router.router)
    app.include_router(export_router.router)
    app.include_router(weave_task_router.router)
    app.include_router(explore_router.router)  # mg-088 物理层自动探查（§2.2）

    mount_profiling(app)

    @app.get("/health")
    async def health():
        return {"status": "ok"}

    return app


app = create_app()
