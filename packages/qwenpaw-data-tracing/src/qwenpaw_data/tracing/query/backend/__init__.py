# -*- coding: utf-8 -*-
"""Backend layer: pick the configured store and open a client for it."""

from __future__ import annotations

import httpx
from qwenpaw_data.tracing.export.settings import Settings
from qwenpaw_data.tracing.query.backend import holoscope, langfuse
from qwenpaw_data.tracing.query.backend.contract import Backend

_TIMEOUT = 30.0


def get_backend() -> tuple[Settings, Backend]:
    settings = Settings.from_env()
    if not settings.configured():
        raise RuntimeError("tracing is not configured")
    name = settings.backend.strip().lower()
    if name == "langfuse":
        return settings, langfuse
    if name == "holoscope":
        return settings, holoscope
    raise ValueError(f"unknown tracing backend: {name}")


def client(settings: Settings) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        base_url=settings.host.rstrip("/"),
        auth=(settings.public_key, settings.secret_key),
        timeout=_TIMEOUT,
    )
