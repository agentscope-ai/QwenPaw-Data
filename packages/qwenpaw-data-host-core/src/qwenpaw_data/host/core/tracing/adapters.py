# -*- coding: utf-8 -*-
"""Host adapters: settings wiring, span exits, session/chat identity, MCP injection."""

from __future__ import annotations

import json
import logging
from collections.abc import Sequence
from typing import TYPE_CHECKING, Any

from qwenpaw_data.host.core.domain.model_usage import ModelUsageContext
from qwenpaw_data.host.core.tracing.utils import (
    attach_biztrace_metadata,
    clear_clarification_pause,
    datasource_and_datasets,
    restore_tool_error_status,
    stamp_skill_name,
)
from qwenpaw_data.tracing import (
    BaggageAttributeProcessor,
    Settings,
)
from qwenpaw_data.tracing import (
    tracer_install as _install,
)
from qwenpaw_data.tracing import (
    tracer_reset as _reset,
)
from qwenpaw_data.tracing import (
    tracer_shutdown as _shutdown,
)
from qwenpaw_data.tracing.core.trace.session import BaseSessionTracer
from qwenpaw_data.tracing.runtime.common.propagation import (
    apply_trace_labels,
    attach_remote_parent,
)
from qwenpaw_data.tracing.runtime.host.chat import (
    tracer as public_tracer,
    tracer_end,
    tracer_span,
    tracer_start as public_tracer_start,
)
from qwenpaw_data.tracing.runtime.host.chat_id import (
    ChatTraceIdGenerator,
    trace_id_from_chat_id,
)
from qwenpaw_data.tracing.runtime.host.metadata import trace_metadata
from opentelemetry.context import Context
from opentelemetry.sdk.trace import ReadableSpan, Span

if TYPE_CHECKING:
    from qwenpaw_data.host.core.runtime.context import RunContext

logger = logging.getLogger(__name__)


# --- settings wiring -------------------------------------------------------- #


def tracer_install() -> None:
    """Register a tracer provider when tracing is configured."""

    settings = Settings.from_env()
    installed = _install(
        settings.to_config(
            service_name=settings.service_name or "qwenpaw-data-host",
            id_generator=ChatTraceIdGenerator(),
            head_processor=_HostSpanProcessor(),
        )
    )
    if installed:
        logger.info("tracing backend=%s", settings.backend)


def tracer_shutdown(timeout_millis: int = 5000) -> None:
    """Flush queued spans and shut the provider down."""

    _shutdown(timeout_millis=timeout_millis)


def tracer_reset() -> None:
    """Test hook. Does not uninstall a process-global provider."""

    _reset()


# --- span exits (AgentScope span flow through here) -------------------------- #


class _HostSpanProcessor(BaggageAttributeProcessor):
    """Host extras: reply trace labels, skill names, clarification cleanup."""

    def on_start_extra(self, span: Span, parent_context: Context | None = None) -> None:
        apply_trace_labels(span)
        stamp_skill_name(span)

    def on_end_extra(self, span: ReadableSpan) -> None:
        clear_clarification_pause(span)
        restore_tool_error_status(span)


# --- session / chat identity ------------------------------------------------ #


class SessionTracer(BaseSessionTracer):
    """Host session identity. A child session hangs on the parent's sessionId."""

    def __init__(
        self,
        *,
        user_id: str,
        session_id: str,
        datasource_id: str | None = None,
        dataset_names: list | None = None,
        parent_session_id: str | None = None,
    ) -> None:
        super().__init__(user_id=user_id, session_id=session_id)
        self._datasource_id = datasource_id
        self._dataset_names = dataset_names
        self._parent_session_id = parent_session_id

    @classmethod
    def from_context(cls, ctx: RunContext) -> SessionTracer:
        datasource_id, dataset_names = datasource_and_datasets(ctx.request_context)
        parent = ctx.request_context.get("parent_session_id")
        parent_id = parent if isinstance(parent, str) and parent.strip() else None
        if parent_id:
            session_id, metadata_parent = parent_id, ctx.session_id
        else:
            session_id, metadata_parent = ctx.session_id, None
        return cls(
            user_id=ctx.identity.user_id,
            session_id=session_id,
            datasource_id=datasource_id,
            dataset_names=dataset_names,
            parent_session_id=metadata_parent,
        )

    def labels(self, *, metadata_json: str | None = None) -> dict[str, str]:
        source = None
        if metadata_json:
            extra = json.loads(metadata_json)
            raw = extra.get("source_trace_id")
            source = raw if isinstance(raw, str) else None
        metadata = trace_metadata(
            datasource_id=self._datasource_id,
            dataset_names=self._dataset_names,
            parent_session_id=self._parent_session_id,
            source_trace_id=source,
        )
        return super().labels(metadata_json=metadata)


async def attach_saved_chat_trace(
    session_factory: Any,
    chat_id: str,
) -> object | None:
    """Attach the chat's saved first-reply span as parent (trace id derives from chat id)."""
    if session_factory is None or not chat_id:
        return None
    from qwenpaw_data.host.core.db.tables import ChatRow

    trace_id = trace_id_from_chat_id(chat_id)
    if trace_id is None:
        return None
    try:
        async with session_factory() as db:
            row = await db.get(ChatRow, chat_id)
    except Exception:
        logger.debug("skip saved trace continuation for %s", chat_id, exc_info=True)
        return None
    if row is None or not row.otel_span_id:
        return None
    return attach_remote_parent(trace_id, row.otel_span_id)


def _host_tracer_kwargs(
    ctx: Any, step: object | None, tags: Sequence[str] | None
) -> dict[str, Any]:
    async def attach() -> object | None:
        return await attach_saved_chat_trace(ctx.session_factory, ctx.chat_id)

    return {
        "chat_id": ctx.chat_id,
        "step": step,
        "session": SessionTracer.from_context(ctx),
        "attach_saved": attach if step is None else None,
        "save_span_id": _tracer_save_span_id(ctx),
        "tags": tags,
    }


def tracer(
    ctx: Any, step: object | None = None, *, tags: Sequence[str] | None = None
) -> Any:
    """For ``async with tracer(...)``. The root opens on enter."""
    return public_tracer(**_host_tracer_kwargs(ctx, step, tags))


def tracer_start(
    ctx: Any, step: object | None = None, *, tags: Sequence[str] | None = None
) -> Any:
    """Open now; pair with ``tracer_end``. Do not use with ``async with``."""
    return public_tracer_start(**_host_tracer_kwargs(ctx, step, tags))


def _tracer_save_span_id(ctx: Any) -> Any:
    if ctx.session_factory is None:
        return None

    async def save(span_id: str) -> None:
        from qwenpaw_data.host.core.db.tables import ChatRow

        async with ctx.session_factory() as db:
            row = await db.get(ChatRow, ctx.chat_id)
            if row is not None and not row.otel_span_id:
                row.otel_span_id = span_id
                await db.commit()

    return save


# --- model wrapping ---------------------------------------------------------- #


def attach_generation_span(model: Any, ctx: ModelUsageContext) -> Any:
    """Span ``generate_structured_output`` unless a chat span is current (AgentScope covers that path)."""

    from qwenpaw_data.tracing.runtime.host.chat import ChatTracer
    from qwenpaw_data.tracing.runtime.host.generation import (
        attach_generation_span as _attach,
    )

    return _attach(
        model,
        default_name=ctx.source_step or ctx.source.value,
        tracer_cls=ChatTracer,
        on_finish=attach_biztrace_metadata,
    )


# --- CM MCP trace injection -------------------------------------------------- #


async def _inject_cm_traceparent(
    run_context: RunContext, cache: dict[int, tuple[str, str | None]]
) -> None:
    """Relay the current trace context onto CM MCP clients (cached)."""
    from qwenpaw_data.host.core.integrations.cm.mcp import is_cm_mcp_client
    from opentelemetry.propagate import inject

    carrier: dict[str, str] = {}
    inject(carrier)
    traceparent = carrier.get("traceparent")
    if not traceparent:
        return
    baggage_value = carrier.get("baggage")
    try:
        clients = await run_context.workspace.list_mcps(
            agent_id=run_context.paths.agent_id,
            session_id=run_context.session_id,
        )
    except Exception:
        logger.debug("mcp list failed while injecting traceparent", exc_info=True)
        return
    live: set[int] = set()
    for client in clients:
        if not is_cm_mcp_client(client):
            continue
        live.add(id(client))
        if cache.get(id(client)) == (traceparent, baggage_value):
            continue
        headers = dict(getattr(client, "_runtime_headers", {}) or {})
        headers["traceparent"] = traceparent
        if baggage_value:
            headers["baggage"] = baggage_value
        try:
            await client.set_runtime_headers(headers)
        except Exception:
            logger.debug(
                "traceparent injection skipped for mcp %s",
                getattr(client, "name", "?"),
                exc_info=True,
            )
            continue
        cache[id(client)] = (traceparent, baggage_value)
    for stale in set(cache) - live:
        del cache[stale]


__all__ = [
    "SessionTracer",
    "attach_saved_chat_trace",
    "tracer",
    "tracer_end",
    "tracer_span",
    "tracer_start",
]
