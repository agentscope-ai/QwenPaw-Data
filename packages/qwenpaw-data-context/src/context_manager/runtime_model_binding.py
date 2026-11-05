"""Request-local DataBridge bindings from an authenticated Host/Engine.

No binding is written to the shared model store or used for embeddings.
"""

from __future__ import annotations

import base64
import json
import os
import secrets
from contextvars import ContextVar

from starlette.responses import JSONResponse
from starlette.datastructures import Headers

HEADER = "x-qwenpaw-model-binding"
llm_binding: ContextVar[dict | None] = ContextVar("llm_binding", default=None)


class RuntimeModelBindingMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        headers = Headers(scope=scope)
        raw = headers.get(HEADER)
        binding = None
        if raw:
            # Only the service credential may supply runtime credentials; a
            # query-only API key must never become a model-routing authority.
            expected = os.environ.get("QWENPAW_DATA_API_TOKEN", "").strip()
            if not expected or not secrets.compare_digest(
                headers.get("authorization", ""), f"Bearer {expected}"
            ):
                return await JSONResponse(
                    {"detail": "Model binding requires the service credential"},
                    status_code=403,
                )(scope, receive, send)
            try:
                if len(raw) > 16384:
                    raise ValueError()
                binding = json.loads(
                    base64.b64decode(raw, altchars=b"-_", validate=True)
                )
                if not isinstance(binding, dict) or set(binding) != {
                    "api_key",
                    "base_url",
                    "model",
                }:
                    raise ValueError()
                if any(
                    not isinstance(v, str) or not v.strip() for v in binding.values()
                ):
                    raise ValueError()
            except (ValueError, TypeError):
                return await JSONResponse(
                    {"detail": "Invalid model binding"}, status_code=422
                )(scope, receive, send)
        token = llm_binding.set(binding)
        try:
            await self.app(scope, receive, send)
        finally:
            llm_binding.reset(token)
