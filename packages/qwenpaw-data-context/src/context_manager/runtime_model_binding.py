"""Request-local DataBridge bindings from an authenticated Host/Engine.

No binding is written to the shared model store or used for embeddings.
"""

from __future__ import annotations

import base64
import json
import os
import secrets
from contextvars import ContextVar
from urllib.parse import urlsplit

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
        if raw is not None:
            # Scoped query keys must never become model-routing authorities.
            expected = os.environ.get("QWENPAW_DATA_API_TOKEN", "").strip()
            if (
                not expected
                or len(headers.getlist("authorization")) != 1
                or not secrets.compare_digest(
                    headers.get("authorization", "").encode("utf-8"),
                    f"Bearer {expected}".encode("utf-8")
                )
            ):
                return await JSONResponse(
                    {"detail": "Model binding requires the service credential"},
                    status_code=403,
                )(scope, receive, send)
            try:
                if len(raw) > 16384 or len(headers.getlist(HEADER)) != 1:
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
                    not isinstance(value, str)
                    or not value.strip()
                    or any(
                        ord(character) < 32 or ord(character) == 127
                        for character in value
                    )
                    for value in binding.values()
                ):
                    raise ValueError()
                if any(
                    binding[name] != binding[name].strip()
                    for name in ("model", "base_url")
                ):
                    raise ValueError()
                endpoint = urlsplit(binding["base_url"])
                if (
                    endpoint.scheme not in {"http", "https"}
                    or not endpoint.hostname
                    or endpoint.username is not None
                    or endpoint.password is not None
                    or endpoint.fragment
                    or (endpoint.port is not None and not 1 <= endpoint.port <= 65535)
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
