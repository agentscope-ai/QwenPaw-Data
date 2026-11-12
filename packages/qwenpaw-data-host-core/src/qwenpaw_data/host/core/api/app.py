# -*- coding: utf-8 -*-
from __future__ import annotations

import asyncio
import os
from contextlib import asynccontextmanager

from qwenpaw_data_auth import AuthConfig, bootstrap_local

from qwenpaw_data.host.core.api.auth_middleware import (
    auth_middleware,
    validate_startup_configuration,
)
from qwenpaw_data.host.core.api.errors import http_exception_handler
from qwenpaw_data.host.core.api.routers import api_router, legacy_api_router
from qwenpaw_data.host.core.channels import ChannelManager
from qwenpaw_data.host.core.cron.manager import CronManager
from qwenpaw_data.host.core.db.engine import get_session_factory, init_db, reset_engine
from qwenpaw_data.host.core.tracing import (
    tracer_install,
    tracer_shutdown,
)
from qwenpaw_data.host.core.tracing.scores import TracerScoreFlusher
from qwenpaw_data.host.core.runtime.recovery import ChatRecovery
from qwenpaw_data.host.core.stream.hub import reset_hub
from qwenpaw_data.host.core.workspace import (
    get_workspace_manager,
    stop_workspace_manager,
)
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware

_CORS_ORIGINS_ENV = "QWENPAW_DATA_CORS_ALLOW_ORIGINS"


def _cors_origins() -> list[str]:
    raw = (os.environ.get(_CORS_ORIGINS_ENV) or "").strip()
    if raw:
        return [origin.strip() for origin in raw.split(",") if origin.strip()]
    return ["http://127.0.0.1", "http://localhost"]


@asynccontextmanager
async def lifespan(app: FastAPI):
    validate_startup_configuration()
    # Identity schema and the local principal come first: Host tables share
    # the control-plane DB and every request is authorized against it.
    app.state.auth_config = await asyncio.to_thread(
        bootstrap_local, AuthConfig.from_env()
    )
    await reset_engine()
    reset_hub()
    await init_db()
    recovery = ChatRecovery(get_session_factory())
    await recovery.start()
    # channel 子系统：按配置启动 enabled channel（无 enabled 则空跑）。
    channel_manager = await ChannelManager.from_config()
    await channel_manager.start_all()
    app.state.channel_manager = channel_manager
    await get_workspace_manager().start()

    cron_manager = CronManager(get_session_factory(), channel_manager)
    await cron_manager.start()
    tracer_install()
    tracer_score_flusher = TracerScoreFlusher(get_session_factory())
    tracer_score_flusher.start()
    try:
        yield
    finally:
        await tracer_score_flusher.shutdown()
        await recovery.shutdown()
        await stop_workspace_manager()
        await cron_manager.shutdown()
        await channel_manager.stop_all()
        tracer_shutdown()
        reset_hub()
        await reset_engine()


def create_app() -> FastAPI:
    app = FastAPI(title="QwenPawData Host", version="0.1.0", lifespan=lifespan)
    app.middleware("http")(auth_middleware)
    # Added after auth so CORS is outermost and 401/403 carry CORS headers.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=_cors_origins(),
        allow_credentials=False,
        allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type", "Last-Event-ID"],
    )
    app.add_exception_handler(HTTPException, http_exception_handler)
    app.include_router(api_router)
    app.include_router(legacy_api_router)

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    return app


app = create_app()
