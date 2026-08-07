"""Unified request resource limits and per-request accounting."""
from __future__ import annotations

import asyncio
import contextvars
import json
import os
import time
from dataclasses import dataclass, field
from typing import Any

from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from .errors import ResourceBudgetExceeded


def _positive_int(name: str, default: int) -> int:
    try:
        return max(1, int(os.getenv(name, str(default))))
    except ValueError:
        return default


def _positive_float(name: str, default: float) -> float:
    try:
        return max(0.001, float(os.getenv(name, str(default))))
    except ValueError:
        return default


@dataclass(frozen=True)
class ResourceLimits:
    request_timeout_seconds: float
    max_upload_bytes: int
    max_response_bytes: int
    max_sql_rows: int
    max_cypher_rows: int
    max_graph_nodes: int
    max_graph_edges: int
    max_llm_tokens: int
    max_llm_retries: int
    max_external_concurrency: int

    @classmethod
    def from_env(cls) -> "ResourceLimits":
        return cls(
            request_timeout_seconds=_positive_float(
                "DATAPAW_REQUEST_TIMEOUT_SECONDS", 120.0
            ),
            max_upload_bytes=_positive_int("DATAPAW_MAX_UPLOAD_MB", 50)
            * 1024
            * 1024,
            max_response_bytes=_positive_int("DATAPAW_MAX_RESPONSE_MB", 50)
            * 1024
            * 1024,
            max_sql_rows=_positive_int("DATAPAW_MAX_SQL_ROWS", 10_000),
            max_cypher_rows=_positive_int("DATAPAW_MAX_CYPHER_ROWS", 1_000),
            max_graph_nodes=_positive_int("DATAPAW_MAX_GRAPH_NODES", 5_000),
            max_graph_edges=_positive_int("DATAPAW_MAX_GRAPH_EDGES", 20_000),
            max_llm_tokens=_positive_int("DATAPAW_MAX_LLM_TOKENS", 32_768),
            max_llm_retries=_positive_int("DATAPAW_MAX_LLM_RETRIES", 4),
            max_external_concurrency=_positive_int(
                "DATAPAW_MAX_EXTERNAL_CONCURRENCY", 16
            ),
        )


def get_resource_limits() -> ResourceLimits:
    """Resolve limits at use time so tests and local overrides remain isolated."""
    return ResourceLimits.from_env()


@dataclass
class RequestBudget:
    limits: ResourceLimits
    started_at: float = field(default_factory=time.monotonic)
    llm_tokens: int = 0
    llm_attempts: int = 0
    external_calls: int = 0

    @property
    def deadline(self) -> float:
        return self.started_at + self.limits.request_timeout_seconds

    def remaining_seconds(self) -> float:
        return max(0.0, self.deadline - time.monotonic())

    def cap_sql_rows(self, requested: int) -> int:
        return min(max(1, requested), self.limits.max_sql_rows)

    def cap_cypher_rows(self, requested: int) -> int:
        return min(max(1, requested), self.limits.max_cypher_rows)

    def cap_graph(self, *, nodes: int, edges: int) -> tuple[int, int]:
        return (
            min(max(1, nodes), self.limits.max_graph_nodes),
            min(max(1, edges), self.limits.max_graph_edges),
        )

    def reserve_llm(self, *, tokens: int, attempts: int = 1) -> None:
        if self.llm_tokens + tokens > self.limits.max_llm_tokens:
            raise ResourceBudgetExceeded(
                "llm_tokens",
                limit=self.limits.max_llm_tokens,
                requested=self.llm_tokens + tokens,
            )
        if self.llm_attempts + attempts > self.limits.max_llm_retries + 1:
            raise ResourceBudgetExceeded(
                "llm_attempts",
                limit=self.limits.max_llm_retries + 1,
                requested=self.llm_attempts + attempts,
            )
        self.llm_tokens += tokens
        self.llm_attempts += attempts

    def record_external_call(self) -> None:
        self.external_calls += 1

    def ensure_response_payload(self, payload: Any) -> int:
        size = len(
            json.dumps(
                payload,
                ensure_ascii=False,
                separators=(",", ":"),
                default=str,
            ).encode("utf-8")
        )
        if size > self.limits.max_response_bytes:
            raise ResourceBudgetExceeded(
                "response_bytes",
                limit=self.limits.max_response_bytes,
                requested=size,
            )
        return size


_budget_var: contextvars.ContextVar[RequestBudget | None] = contextvars.ContextVar(
    "datapaw_request_budget",
    default=None,
)


def current_request_budget() -> RequestBudget:
    budget = _budget_var.get()
    if budget is None:
        budget = RequestBudget(get_resource_limits())
    return budget


def budget_error_payload(error: ResourceBudgetExceeded) -> dict[str, Any]:
    return {
        "error": {
            "code": error.code,
            "message": error.message,
            "retryable": error.retryable,
            "details": error.details,
        }
    }


class ResourceBudgetMiddleware:
    """Apply a deadline across the complete ASGI response lifecycle."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        budget = RequestBudget(get_resource_limits())
        token = _budget_var.set(budget)
        response_started = False

        async def tracked_send(message: dict[str, Any]) -> None:
            nonlocal response_started
            if message["type"] == "http.response.start":
                response_started = True
            await send(message)

        try:
            async with asyncio.timeout(budget.limits.request_timeout_seconds):
                await self.app(scope, receive, tracked_send)
        except TimeoutError:
            if response_started:
                raise
            error = ResourceBudgetExceeded(
                "request_time",
                limit=budget.limits.request_timeout_seconds,
                http_status=504,
            )
            response = JSONResponse(
                status_code=504,
                content=budget_error_payload(error),
                headers={"Retry-After": "1"},
            )
            await response(scope, receive, send)
        finally:
            _budget_var.reset(token)
