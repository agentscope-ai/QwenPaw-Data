"""Shared helpers for workspace graph provisioning (Neo4j vs Holo AGE)."""

from __future__ import annotations

import os
from typing import Any

from fastapi.responses import JSONResponse

HOLO_BACKEND = "holo_age"
NEO4J_BACKEND = "neo4j"
SUPPORTED_BACKENDS = {NEO4J_BACKEND, HOLO_BACKEND}


def _env_set(name: str) -> bool:
    return bool(os.environ.get(name, "").strip())


def _parse_backend_list(raw: str) -> list[str]:
    seen: list[str] = []
    for item in raw.split(","):
        name = item.strip().lower()
        if name in SUPPORTED_BACKENDS and name not in seen:
            seen.append(name)
    return seen


def graph_backend() -> str:
    """Active graph engine from ``GRAPH_BACKEND`` (default Neo4j)."""
    raw = (os.environ.get("GRAPH_BACKEND") or NEO4J_BACKEND).strip().lower()
    if raw in {"holoage", "holo-age"}:
        raw = HOLO_BACKEND
    return raw if raw in SUPPORTED_BACKENDS else NEO4J_BACKEND


def configured_provision_backends() -> list[str]:
    """Backends this deployment can provision.

    Priority:
    1. ``QWENPAW_DATA_PROVISION_BACKENDS=neo4j,holo_age`` (explicit)
    2. Credentials already present (``NEO4J_*`` / ``HOLO_AGE_*``)
    3. ``GRAPH_BACKEND`` so the active engine is always an option
    """
    explicit = _parse_backend_list(os.environ.get("QWENPAW_DATA_PROVISION_BACKENDS", ""))
    if explicit:
        return explicit

    default = graph_backend()
    found: list[str] = [default]
    if default != NEO4J_BACKEND and (
        _env_set("NEO4J_URI") or _env_set("NEO4J_PASSWORD")
    ):
        found.append(NEO4J_BACKEND)
    if (
        default != HOLO_BACKEND
        and _env_set("HOLO_AGE_HOST")
        and _env_set("HOLO_AGE_PASSWORD")
    ):
        found.append(HOLO_BACKEND)
    return found


def default_provision_backend(available: list[str] | None = None) -> str:
    available = available if available is not None else configured_provision_backends()
    desired = graph_backend()
    if desired in available:
        return desired
    return available[0] if available else NEO4J_BACKEND


def parse_provision_backend(body: Any) -> str | JSONResponse:
    """Read ``backend`` from a provision request. Default is ``GRAPH_BACKEND``."""
    available = configured_provision_backends()
    if not isinstance(body, dict) or not str(body.get("backend") or "").strip():
        return default_provision_backend(available)
    raw = str(body.get("backend")).strip().lower()
    if raw not in SUPPORTED_BACKENDS:
        return JSONResponse(
            status_code=400,
            content={"detail": "backend must be neo4j or holo_age"},
        )
    if raw not in available:
        return JSONResponse(
            status_code=400,
            content={"detail": f"backend {raw} is not configured"},
        )
    return raw


def queue_workspace_values(backend: str) -> dict[str, Any]:
    """Values written when a workspace is queued for provisioning."""
    return {
        "instance_status": "queued",
        "status": "provisioning",
        "instance_version": HOLO_BACKEND if backend == HOLO_BACKEND else None,
    }
