"""Dispatch HTTP requests to the Auth app or the Context app by path.

A single-process deployment runs one uvicorn process. Auth routes must win
over Context's local ``GET /api/auth/status`` stub so login discovery reaches
the real Auth service.
"""

from __future__ import annotations

import traceback
from contextlib import AsyncExitStack
from typing import Any, Awaitable, Callable

Scope = dict[str, Any]
Receive = Callable[[], Awaitable[dict[str, Any]]]
Send = Callable[[dict[str, Any]], Awaitable[None]]
ASGIApp = Callable[[Scope, Receive, Send], Awaitable[None]]

# Longest-prefix match is not required: each entry is either an exact path or
# a directory prefix followed by "/". ``/api/userspace`` therefore does not
# collide with ``/api/users``.
_AUTH_PREFIXES = (
    "/api/auth",
    "/api/platform",
    "/api/users",
    "/api/roles",
    "/api/tenants",
    "/api/workspaces",
    "/api/access-requests",
)


def is_auth_path(path: str) -> bool:
    """Whether this URL belongs to the Auth FastAPI app."""
    for prefix in _AUTH_PREFIXES:
        if path == prefix or path.startswith(prefix + "/"):
            return True
    return False


class DualASGI:
    """Run two FastAPI apps in one process without stripping path prefixes.

    Lifespan of both apps is entered so Auth can ``init_schema`` and Context
    can open Neo4j. HTTP/WebSocket requests are forwarded intact.
    """

    def __init__(self, auth_app: ASGIApp, context_app: ASGIApp) -> None:
        self.auth_app = auth_app
        self.context_app = context_app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "lifespan":
            await self._lifespan(scope, receive, send)
            return
        path = scope.get("path") or ""
        target = self.auth_app if is_auth_path(path) else self.context_app
        await target(scope, receive, send)

    async def _lifespan(self, scope: Scope, receive: Receive, send: Send) -> None:
        message = await receive()
        if message["type"] != "lifespan.startup":
            return
        async with AsyncExitStack() as stack:
            try:
                await stack.enter_async_context(
                    self.auth_app.router.lifespan_context(self.auth_app)  # type: ignore[attr-defined]
                )
                await stack.enter_async_context(
                    self.context_app.router.lifespan_context(self.context_app)  # type: ignore[attr-defined]
                )
            except BaseException:
                await send(
                    {
                        "type": "lifespan.startup.failed",
                        "message": traceback.format_exc(),
                    }
                )
                raise
            await send({"type": "lifespan.startup.complete"})
            while True:
                message = await receive()
                if message["type"] == "lifespan.shutdown":
                    break
        await send({"type": "lifespan.shutdown.complete"})
