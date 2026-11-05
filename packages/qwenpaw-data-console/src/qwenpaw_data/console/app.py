"""Loopback chat and official DataBridge UIs with server-side API credentials."""

from contextlib import AsyncExitStack, asynccontextmanager
import json
from pathlib import Path
import re
from urllib.parse import urlsplit

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import (
    FileResponse,
    JSONResponse,
    RedirectResponse,
    Response,
    StreamingResponse,
)
from starlette.background import BackgroundTask
from starlette.middleware.trustedhost import TrustedHostMiddleware

from .config import ConsoleConfig

STATIC = Path(__file__).parent / "static"
FRONTEND = STATIC / "chat"
BRIDGE_FRONTEND = STATIC / "databridge"
ROUTES = re.compile(
    r"^(auth/status|datasources|datasets|"
    r"console/(chat(?:/stop)?|upload|attachments/[A-Za-z0-9_-]+|capabilities)|"
    r"preferences/(providers(?:/[^/]+(?:/models(?:/[^/]+)?)?)?|active-models|runtime-settings)|"
    r"cron/jobs(?:/[A-Za-z0-9_-]+(?:/(pause|resume|run))?)?|"
    r"sessions(?:/[A-Za-z0-9_-]+(?:/(snapshot|fork|skill-drafts)|"
    r"/chats(?:/[A-Za-z0-9_-]+(?:/(events|trace|stop|steer|clarification/answer|plan/edit|feedback(?:/reaction)?))?)?|"
    r"/settlement/(cards(?:/[A-Za-z0-9_-]+/(confirm|dismiss))?|skill-proposals)|"
    r"/artifacts(?:/file)?|/files/access)?)?)$"
)


def create_app(
    *,
    host_url,
    token,
    config=None,
    routers=(),
    transport=None,
    frontend=None,
    bridge_url=None,
    bridge_token=None,
    bridge_frontend=None,
    bridge_transport=None,
):
    """Serve packaged UIs and forward local APIs without exposing service tokens.

    Host and DataBridge keep their own lifecycles. Optional transports and asset
    roots support embedding, frontend development, and contract tests.
    """
    config = config or ConsoleConfig()
    browser_config = config.browser_config(databridge=bool(bridge_url))
    frontend = Path(frontend or FRONTEND).resolve()
    if not (frontend / "index.html").is_file():
        raise ValueError(
            "Console assets missing; install the built wheel or run scripts/build_console.py in QwenPaw-Data"
        )
    origin = urlsplit(host_url)
    if (
        origin.scheme != "http"
        or origin.hostname not in {"127.0.0.1", "localhost"}
        or origin.username
        or origin.password
        or origin.path not in ("", "/")
        or origin.query
        or origin.fragment
    ):
        raise ValueError("Host API must be on loopback")
    if not token:
        raise ValueError("Host API token is required")
    if bridge_url:
        origin = urlsplit(bridge_url)
        if (
            origin.scheme != "http"
            or origin.hostname not in {"127.0.0.1", "localhost"}
            or origin.username
            or origin.password
            or origin.path not in ("", "/")
            or origin.query
            or origin.fragment
        ):
            raise ValueError("DataBridge API must be on loopback")
        bridge_url = bridge_url.rstrip("/")
        bridge_frontend = Path(bridge_frontend or BRIDGE_FRONTEND).resolve()
        if not (bridge_frontend / "index.html").is_file():
            raise ValueError(
                "DataBridge assets missing; install the built Console wheel or run scripts/build_console.py"
            )

    @asynccontextmanager
    async def lifespan(app):
        async with AsyncExitStack() as stack:
            client = await stack.enter_async_context(
                httpx.AsyncClient(
                    base_url=host_url,
                    transport=transport,
                    headers={"Authorization": "Bearer " + token},
                    timeout=httpx.Timeout(30, read=None),
                    follow_redirects=False,
                )
            )
            app.state.client = client
            if bridge_url:
                app.state.bridge_client = await stack.enter_async_context(
                    httpx.AsyncClient(
                        base_url=bridge_url,
                        transport=bridge_transport,
                        headers={"Authorization": "Bearer " + (bridge_token or token)},
                        timeout=httpx.Timeout(30, read=None),
                        follow_redirects=False,
                    )
                )
            yield

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.add_middleware(
        TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost", "testserver"]
    )

    @app.middleware("http")
    async def local_access(request, call_next):
        if request.client and request.client.host not in {
            "127.0.0.1",
            "::1",
            "testclient",
        }:
            return Response("Loopback only", status_code=403)
        if request.url.path.startswith(("/api/", "/portal/", "/databridge/api/")):
            # A custom header plus same-origin checks prevent cross-site pages
            # from using this local proxy as an authenticated API relay.
            # The Console opens HTML previews in a new tab. Permit only same-origin
            # navigations to the guarded file endpoint; sandbox HTML below.
            same_origin_file = (
                request.method == "GET"
                and (
                    request.url.path.endswith("/files/access")
                    or re.fullmatch(
                        r"/databridge/api/v1/docs/[^/]+/download", request.url.path
                    )
                )
                and request.headers.get("sec-fetch-site") == "same-origin"
                and request.headers.get("referer", "").startswith(str(request.base_url))
            )
            if request.headers.get("x-qwenpaw-console") != "1" and not same_origin_file:
                return Response("Portal header required", status_code=403)
            if request.headers.get("origin") not in (
                None,
                str(request.base_url).rstrip("/"),
            ):
                return Response("Origin rejected", status_code=403)
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "same-origin"
        response.headers["X-Frame-Options"] = "SAMEORIGIN"
        if request.url.path.startswith(
            "/portal/"
        ) and "text/html" in response.headers.get("content-type", ""):
            response.headers["Content-Security-Policy"] = (
                "sandbox allow-scripts allow-downloads"
            )
        return response

    @app.api_route(
        "/databridge/api/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE"]
    )
    async def bridge_proxy(path, request: Request):
        if (
            not bridge_url
            or not path
            or "\\" in path
            or any(p in {"", ".", ".."} for p in path.rstrip("/").split("/"))
        ):
            raise HTTPException(404, "Unsupported DataBridge route")
        # Never allow an encoded path to escape the fixed upstream /api namespace.
        if "%" in path or "//" in path or path.startswith("/"):
            raise HTTPException(404, "Unsupported DataBridge route")
        client = request.app.state.bridge_client
        try:
            if path == "auth/status" and request.method == "GET":
                # The local portal authenticates to DataBridge on the user's behalf.
                # Verify that credential before allowing the UI to enter.
                checked = await client.get("/api/auth/check")
                if checked.status_code != 200:
                    raise HTTPException(
                        502,
                        "DataBridge authentication failed; check the configured service credential",
                    )
                return {"required": False}
            upstream = client.build_request(
                request.method,
                "/api/" + path,
                params=request.query_params,
                content=await request.body(),
                headers={
                    "Content-Type": request.headers.get(
                        "content-type", "application/json"
                    )
                },
            )
            response = await client.send(upstream, stream=True)
        except httpx.HTTPError:
            raise HTTPException(
                502, "DataBridge unavailable; start the configured DataBridge service"
            ) from None
        headers = {
            k: response.headers[k]
            for k in ("content-type", "content-disposition", "retry-after")
            if k in response.headers
        }
        if "application/json" in response.headers.get("content-type", ""):
            try:
                payload = json.loads(await response.aread())
            finally:
                await response.aclose()

            def local_urls(value):
                if isinstance(value, dict):
                    return {k: local_urls(v) for k, v in value.items()}
                if isinstance(value, list):
                    return [local_urls(v) for v in value]
                if isinstance(value, str) and value.startswith(bridge_url + "/api/"):
                    return "/databridge" + value[len(bridge_url) :]
                return value

            return JSONResponse(
                local_urls(payload), status_code=response.status_code, headers=headers
            )
        if "text/html" in headers.get("content-type", ""):
            headers["Content-Security-Policy"] = "sandbox allow-scripts allow-downloads"
        return StreamingResponse(
            response.aiter_bytes(),
            status_code=response.status_code,
            headers=headers,
            background=BackgroundTask(response.aclose),
        )

    @app.get("/databridge")
    async def bridge_redirect():
        return RedirectResponse("/databridge/")

    @app.get("/databridge/{path:path}")
    async def bridge_page(path):
        if not bridge_url or path.startswith("api/"):
            raise HTTPException(404)
        candidate = (bridge_frontend / path).resolve()
        if not candidate.is_relative_to(bridge_frontend):
            raise HTTPException(404)
        if candidate.is_file():
            return FileResponse(candidate)
        if path.startswith("assets/") or Path(path).suffix:
            raise HTTPException(404)
        return FileResponse(bridge_frontend / "index.html")

    @app.get("/portal/config")
    async def public_config():
        return browser_config

    @app.get("/console-config.js")
    async def config_script():
        # JSON, never interpolated HTML or arbitrary executable configuration.
        payload = json.dumps(browser_config, ensure_ascii=True).replace("<", "\\u003c")
        return Response(
            "window.QWENPAW_DATA_CONSOLE = " + payload + ";",
            media_type="application/javascript",
        )

    @app.api_route(
        "/api/v1/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE"]
    )
    async def proxy(path, request: Request):
        if not ROUTES.fullmatch(path):
            raise HTTPException(404, "Unsupported portal route")
        try:
            upstream = request.app.state.client.build_request(
                request.method,
                "/api/v1/" + path,
                params=request.query_params,
                content=await request.body(),
                headers={
                    "Content-Type": request.headers.get(
                        "content-type", "application/json"
                    ),
                    **(
                        {"Last-Event-ID": request.headers["last-event-id"]}
                        if "last-event-id" in request.headers
                        else {}
                    ),
                },
            )
            response = await request.app.state.client.send(upstream, stream=True)
        except httpx.HTTPError:
            raise HTTPException(
                502, "Host unavailable; start the configured Host service"
            ) from None
        headers = {
            k: response.headers[k]
            for k in ("content-type", "content-disposition", "retry-after")
            if k in response.headers
        }
        if "text/html" in response.headers.get("content-type", ""):
            headers["Content-Security-Policy"] = "sandbox allow-scripts allow-downloads"
        return StreamingResponse(
            response.aiter_bytes(),
            status_code=response.status_code,
            headers=headers,
            background=BackgroundTask(response.aclose),
        )

    for router in routers:
        app.include_router(router)

    @app.get("/{path:path}")
    async def frontend_file(path):
        if path.startswith(("api/", "portal/")):
            raise HTTPException(404)
        candidate = (frontend / path).resolve()
        if candidate.is_relative_to(frontend) and candidate.is_file():
            return FileResponse(candidate)
        if path == "" or path == "console" or path.startswith("console/"):
            return FileResponse(frontend / "index.html")
        raise HTTPException(404)

    return app
