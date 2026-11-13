"""Build the CM FastAPI shell and bind its runtime ownership explicitly."""

from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from qwenpaw_data.context.blocking_io import BlockingIOOverloaded, BlockingIOTimeout
from qwenpaw_data_auth.authorization import authorize_request
from qwenpaw_data_auth.config import AuthConfig
from qwenpaw_data_auth.local import bootstrap_local
from qwenpaw_data_auth.principal import DEFAULT_PRINCIPAL

from ..mcp import mount_mcp
from ..runtime import ContextRuntime, ContextRuntimeUnavailable
from ..runtime_model_binding import RuntimeModelBindingMiddleware
from ..runtime.connection import (
    GraphConnectionConfig,
    HoloConnectionConfig,
    Neo4jConnectionConfig,
)
from ..runtime.startup import (
    DependencyInitializer,
    DriverFactory,
    create_service_runtime,
    initialize_semantic_stores,
)
from ..settings import get_settings
from ..settings.cron import CronSettings
from ..utils.redaction import install_credential_redaction
from .authentication import Authorizer, install_context_authentication
from .health import router as health_router
from .legacy import router as legacy_router
from .lifespan import api_lifespan
from .native.router import mount_native_routes


async def _runtime_unavailable(
    request: Request, exc: ContextRuntimeUnavailable
) -> JSONResponse:
    """Preserve the HTTP failure contract when a handler selects its Workspace."""
    return JSONResponse(status_code=503, content={"detail": str(exc)})


async def _blocking_io_overloaded(
    _request: Request, exc: BlockingIOOverloaded
) -> JSONResponse:
    """Expose bounded-worker saturation as a retryable service response."""
    return JSONResponse(
        status_code=503,
        headers={"Retry-After": "1"},
        content={
            "code": "BLOCKING_IO_OVERLOADED",
            "detail": str(exc),
            "pool": exc.pool.value,
            "operation": exc.operation,
        },
    )


async def _blocking_io_timeout(
    _request: Request, exc: BlockingIOTimeout
) -> JSONResponse:
    """Expose bounded-worker timeouts separately from admission overload."""
    return JSONResponse(
        status_code=504,
        content={
            "code": "BLOCKING_IO_TIMEOUT",
            "detail": str(exc),
            "pool": exc.pool.value,
            "operation": exc.operation,
        },
    )


def mount_context_routes(application: FastAPI) -> None:
    """Mount the finite CM route surface exactly once."""
    if not isinstance(application, FastAPI):
        raise TypeError("application must be a FastAPI app")
    if getattr(application.state, "context_routes_mounted", False):
        raise RuntimeError("context routes are already mounted")
    application.add_exception_handler(ContextRuntimeUnavailable, _runtime_unavailable)
    application.include_router(health_router)
    mount_native_routes(application)
    # TODO(cm-api-compat): remove this mount after every legacy frontend caller
    # has migrated and context_manager.api.legacy is deleted.
    application.include_router(legacy_router)
    mount_mcp(application)
    application.state.context_routes_mounted = True


def bind_context_runtime(
    application: FastAPI,
    runtime: ContextRuntime,
) -> None:
    """Bind one process runtime without taking over a host application's lifespan."""
    if not isinstance(application, FastAPI):
        raise TypeError("application must be a FastAPI app")
    if type(runtime) is not ContextRuntime or runtime.closed:
        raise TypeError("runtime must be an open ContextRuntime")
    if hasattr(application.state, "context_runtime"):
        raise RuntimeError("context runtime is already bound")
    application.state.context_runtime = runtime


def create_api_app() -> FastAPI:
    """Create the CM API shell without selecting a runtime or installing auth."""
    application = FastAPI(
        title="Context Manager",
        version="1.0",
        lifespan=api_lifespan,
    )
    application.add_exception_handler(BlockingIOOverloaded, _blocking_io_overloaded)
    application.add_exception_handler(BlockingIOTimeout, _blocking_io_timeout)
    application.add_middleware(RuntimeModelBindingMiddleware)
    mount_context_routes(application)
    return application


def local_connection() -> GraphConnectionConfig:
    """Bind local mode to the reserved Workspace seeded by local bootstrap."""
    settings = get_settings()
    tenant_id = DEFAULT_PRINCIPAL.tenant_id
    workspace_id = DEFAULT_PRINCIPAL.active_workspace_id
    if settings.graph.backend == "holoage":
        from ..store.holoage.target import provisionable_graph_name

        return HoloConnectionConfig(
            tenant_id=tenant_id,
            workspace_id=workspace_id,
            graph_name=provisionable_graph_name(workspace_id),
        )
    route = settings.runtime
    if not route.neo4j_password:
        raise RuntimeError("NEO4J_PASSWORD is required for the local graph")
    return Neo4jConnectionConfig(
        tenant_id=tenant_id, workspace_id=workspace_id, **route.model_dump()
    )


def create_service_app(
    connection: GraphConnectionConfig | None = None,
    auth_config: AuthConfig | None = None,
    *,
    driver_factory: DriverFactory | None = None,
    authorizer: Authorizer = authorize_request,
    semantic_initializer: DependencyInitializer | None = initialize_semantic_stores,
    cron_settings: CronSettings | None = None,
) -> FastAPI:
    """Assemble the authenticated HTTP service around one process Runtime."""
    from ..metadata.weaving.integration import mount_editor

    install_credential_redaction()
    security_config = bootstrap_local(auth_config or get_settings().auth)
    if connection is None and security_config.local_mode:
        connection = local_connection()
    application = create_api_app()
    runtime = create_service_runtime(
        connection,
        driver_factory=driver_factory,
        semantic_initializer=semantic_initializer,
        cron_settings=cron_settings,
    )
    try:
        bind_context_runtime(application, runtime)
        mount_editor(application)
        install_context_authentication(
            application,
            security_config,
            runtime.fixed_target,
            authorizer=authorizer,
        )
        if security_config.cors_allowed_origins:
            application.add_middleware(
                CORSMiddleware,
                allow_origins=security_config.cors_allowed_origins,
                allow_credentials=True,
                allow_methods=["*"],
                allow_headers=["*"],
            )
        return application
    except Exception:
        runtime.close()
        raise


__all__ = [
    "bind_context_runtime",
    "create_api_app",
    "create_service_app",
    "local_connection",
    "mount_context_routes",
]
