"""Auth FastAPI application — the main entry point for the Auth service."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from urllib.parse import urlsplit

from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from qwenpaw_data_auth.config import AuthConfig
from qwenpaw_data_auth.database import (
    get_db,
    query_one,
    tenants,
    workspaces,
)
from qwenpaw_data_auth.local import bootstrap_local, is_safe_loopback_request
from sqlalchemy import insert, select

logger = logging.getLogger("qwenpaw_data_auth")


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup, not import, creates runtime state: the runtime home, generated
    # secrets persisted there and the schema. Local mode also seeds the single
    # local administrator; multi-user mode never does.
    app.state.auth_config = bootstrap_local(app.state.auth_config)
    cfg: AuthConfig = app.state.auth_config

    if cfg.auth_enabled:
        if cfg.allow_legacy_first_user_registration:
            with get_db(cfg.control_plane_dsn) as conn:
                tenant = query_one(
                    conn, select(tenants.c.id).where(tenants.c.id == "default")
                )
                if not tenant:
                    conn.execute(
                        insert(tenants).values(
                            id="default",
                            name="Default Tenant",
                            neo4j_database="neo4j",
                        )
                    )
                    conn.commit()
                    logger.info("Created default tenant")

                ws = query_one(
                    conn, select(workspaces.c.id).where(workspaces.c.id == "default")
                )
                if not ws:
                    conn.execute(
                        insert(workspaces).values(
                            id="default",
                            tenant_id="default",
                            name="Default Workspace",
                            description="System default workspace",
                        )
                    )
                    conn.commit()
                    logger.info("Created default workspace")
    else:
        logger.warning(
            "Auth runs in local single-user mode: only safe loopback clients act "
            "as the local administrator. Set QWENPAW_DATA_AUTH_MODE=multi-user "
            "to require sign-in."
        )

    yield


def _origin(value: str) -> str | None:
    """Normalize a URL to its origin; ``None`` when it is not a web origin."""
    try:
        parsed = urlsplit(value)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            return None
        if parsed.username is not None or parsed.password is not None:
            return None
        host = f"[{parsed.hostname}]" if ":" in parsed.hostname else parsed.hostname
        origin = f"{parsed.scheme}://{host}"
        if parsed.port is not None:
            origin += f":{parsed.port}"
        return origin
    except ValueError:
        return None


def create_app(config: AuthConfig | None = None) -> FastAPI:
    cfg = config or AuthConfig.from_env()
    if cfg.env == "production":
        if not cfg.auth_enabled:
            raise RuntimeError(
                "QWENPAW_DATA_AUTH_MODE=multi-user is required in production"
            )
        if not cfg.secret_keys:
            raise RuntimeError("QWENPAW_DATA_AUTH_SECRET is required in production")
        if not cfg.control_plane_dsn:
            raise RuntimeError("QWENPAW_DATA_DB_URL is required in production")
        if not cfg.cors_allowed_origins:
            raise RuntimeError("CORS_ALLOWED_ORIGINS is required in production")
    # Configured origins receive credentialed CORS; a wildcard there would let
    # any site read signed-in responses.
    if "*" in cfg.cors_allowed_origins:
        raise ValueError("Credentialed CORS requires explicit origins")

    app = FastAPI(title="QwenPawData Auth", version="0.1.0", lifespan=lifespan)
    app.state.auth_config = cfg

    # Suppress noisy external polling requests (e.g. Dify service worker residue)
    # from uvicorn's access log to avoid 404 spam in console output.
    class _SuppressExternalPollingFilter(logging.Filter):
        _SUPPRESSED = ("/api/console/",)

        def filter(self, record: logging.LogRecord) -> bool:
            msg = record.getMessage()
            return not any(p in msg for p in self._SUPPRESSED)

    logging.getLogger("uvicorn.access").addFilter(_SuppressExternalPollingFilter())

    origins = cfg.cors_allowed_origins if cfg.cors_allowed_origins else ["*"]
    app.add_middleware(
        CORSMiddleware,
        allow_origins=origins,
        allow_credentials=bool(cfg.cors_allowed_origins),
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.middleware("http")
    async def csrf_guard(request: Request, call_next):
        """Reject state-changing requests that a foreign origin initiated.

        Cookies are ``SameSite=lax``, which still lets another port of the same
        host post with them; the declared origin must be this service, a
        configured CORS origin, or a safe loopback client in local mode.
        """
        current: AuthConfig = app.state.auth_config
        if request.method not in {"GET", "HEAD", "OPTIONS"}:
            declared = request.headers.getlist("origin")
            referers = request.headers.getlist("referer")
            if len(declared) > 1 or len(referers) > 1:
                return JSONResponse(
                    status_code=403, content={"detail": "Ambiguous request origin"}
                )
            supplied = declared[0] if declared else referers[0] if referers else None
            if supplied is not None:
                source = _origin(supplied)
                allowed = source is not None and (
                    source == _origin(str(request.base_url))
                    or source in current.cors_allowed_origins
                )
                if current.local_mode and is_safe_loopback_request(request):
                    allowed = True
                if not allowed:
                    return JSONResponse(
                        status_code=403,
                        content={"detail": "Request origin is not allowed"},
                    )
            elif request.headers.get("sec-fetch-site") == "cross-site":
                return JSONResponse(
                    status_code=403, content={"detail": "Cross-site request denied"}
                )
        return await call_next(request)

    from qwenpaw_data_auth.auth_api import router as auth_router
    from qwenpaw_data_auth.tenant_api import router as tenant_router
    from qwenpaw_data_auth.workspace_api import router as workspace_router
    from qwenpaw_data_auth.user_api import router as user_router
    from qwenpaw_data_auth.role_api import router as role_router
    from qwenpaw_data_auth.platform_api import router as platform_router
    from qwenpaw_data_auth.access_request_api import router as access_request_router

    app.include_router(auth_router)
    app.include_router(tenant_router)
    app.include_router(workspace_router)
    app.include_router(user_router)
    app.include_router(role_router)
    app.include_router(platform_router)
    app.include_router(access_request_router)

    @app.get("/api/health")
    async def health():
        return {"status": "ok", "auth_enabled": cfg.auth_enabled, "mode": cfg.mode}

    @app.api_route(
        "/api/console/{path:path}", methods=["GET", "POST"], include_in_schema=False
    )
    async def ignore_console_polling() -> Response:
        return Response(status_code=204)

    import os

    frontend_dist = os.path.join(
        os.path.dirname(__file__), "..", "..", "frontend", "dist"
    )
    if os.path.isdir(frontend_dist):
        app.mount("/", StaticFiles(directory=frontend_dist, html=True), name="frontend")

    return app


app = create_app()
