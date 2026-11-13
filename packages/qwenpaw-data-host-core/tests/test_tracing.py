# -*- coding: utf-8 -*-
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest
from qwenpaw_data_auth import Principal
from qwenpaw_data.host.core.agent.middleware.tracing import (
    TracingMiddleware,
    stamp_tool_state,
)
from qwenpaw_data.host.core.api.auth_middleware import principal_ctx
from qwenpaw_data.host.core.domain.identity import Identity
from qwenpaw_data.host.core.domain.model_usage import ModelUsageContext, ModelUsageSource
from qwenpaw_data.host.core.domain.preference import ActiveModel
from qwenpaw_data.host.core.providers.factory import build_model
from qwenpaw_data.host.core.tracing.adapters import (
    _ctx_fields,
    _HostSpanProcessor,
    _inject_cm_traceparent,
    attach_generation_span,
    attach_saved_chat_trace,
)
from qwenpaw_data.host.core.tracing.adapters import (
    tracer as host_tracer,
)
from qwenpaw_data.host.core.tracing.records import (
    normalize_question,
    tracer_record_behavior,
)
from qwenpaw_data.host.core.tracing.scores import (
    MAX_DELIVERY_ATTEMPTS,
    flush_scores,
)
from qwenpaw_data.host.core.tracing.utils import (
    attach_biztrace_metadata,
    skill_name_from_arguments,
)
from qwenpaw_data.host.core.utils.ids import create_id
from qwenpaw_data.tracing import (
    DebugSpanProcessor,
    reset_warn_once,
)
from qwenpaw_data.tracing.core.trace.handles import _Generation
from qwenpaw_data.tracing.runtime.common.propagation import (
    bind_trace_labels,
    reset_trace_labels,
)
from qwenpaw_data.tracing.runtime.host.chat import (
    ChatTracer,
    tracer_end,
    tracer_session,
    tracer_span,
    tracer_start,
)
from qwenpaw_data.tracing.runtime.host.chat_id import (
    ChatTraceIdGenerator,
    bind_chat_trace,
    reset_chat_trace,
    trace_id_from_chat_id,
)
from opentelemetry import baggage, context
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import StatusCode


def test_normalize_question_collapses_whitespace() -> None:
    assert normalize_question("  本月  销售额\n是多少  ") == "本月 销售额 是多少"


def test_create_id_chat_is_prefix_plus_trace_id() -> None:
    chat_id = create_id("chat")
    trace_id = trace_id_from_chat_id(chat_id)
    assert trace_id is not None
    assert chat_id == f"chat-{trace_id}"
    assert len(trace_id) == 32


def test_new_trace_opens_an_independent_root(monkeypatch: pytest.MonkeyPatch) -> None:
    provider = TracerProvider(id_generator=ChatTraceIdGenerator())
    provider.add_span_processor(_HostSpanProcessor())
    exporter = InMemorySpanExporter()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    otel = provider.get_tracer("test")
    monkeypatch.setattr(
        "opentelemetry.trace.get_tracer",
        lambda *_a, **_k: otel,
    )
    chat_id = "chat-f33fa5fa9c8b4cb28ae4055f394f3ee5"
    source_trace_id = chat_id[5:]
    chat_token = bind_chat_trace(chat_id)
    try:
        with otel.start_as_current_span("invoke_agent qwenpaw-data") as reply_span:
            reply_trace_id = format(reply_span.get_span_context().trace_id, "032x")
            assert reply_trace_id == source_trace_id
            child = tracer_start(
                tracer_session("u1", "ses-1"),
                chat_id=chat_id,
                step="settlement",
            )
            assert child.trace_id != reply_trace_id
            assert child._source_trace_id == source_trace_id
            with child:
                with tracer_span("tick"):
                    pass
            tracer_end(child)
            with otel.start_as_current_span("followup") as span:
                assert (
                    format(span.get_span_context().trace_id, "032x") == reply_trace_id
                )
    finally:
        reset_chat_trace(chat_token)

    root = next(s for s in exporter.get_finished_spans() if s.name == "settlement")
    assert root.parent is None
    assert format(root.context.trace_id, "032x") != source_trace_id
    assert root.attributes["session.id"] == "ses-1"
    assert root.attributes["langfuse.trace.name"] == "settlement"
    metadata = json.loads(root.attributes["langfuse.trace.metadata"])
    assert metadata == {"source_trace_id": source_trace_id}


def test_processor_stamps_model_and_skill_without_chat_id() -> None:
    provider = TracerProvider()
    provider.add_span_processor(_HostSpanProcessor())
    tracer = provider.get_tracer("test")
    ctx = baggage.set_baggage(
        "langfuse.trace.metadata",
        json.dumps({"datasource_id": "ds-1"}),
    )
    ctx = baggage.set_baggage("langfuse.trace.metadata.chat_id", "chat-1", context=ctx)
    token = context.attach(ctx)
    try:
        with tracer.start_as_current_span(
            "execute_tool Skill",
            attributes={
                "gen_ai.operation.name": "chat",
                "gen_ai.request.model": "qwen",
                "gen_ai.tool.name": "Skill",
                "gen_ai.tool.call.arguments": '{"skill": "sql"}',
            },
        ) as span:
            assert json.loads(span.attributes["langfuse.trace.metadata"]) == {
                "datasource_id": "ds-1"
            }
            assert "langfuse.trace.metadata.chat_id" not in span.attributes
            assert span.attributes["langfuse.observation.type"] == "generation"
            assert span.attributes["langfuse.observation.model.name"] == "qwen"
            assert span.attributes["skill.name"] == "sql"
    finally:
        context.detach(token)


def test_trace_labels_replace_agentscope_conversation_id() -> None:
    provider = TracerProvider()
    provider.add_span_processor(_HostSpanProcessor())
    tracer = provider.get_tracer("test")
    token = bind_trace_labels(
        {
            "session.id": "ses-parent",
            "langfuse.session.id": "ses-parent",
            "gen_ai.conversation.id": "ses-parent",
            "user.id": "user-1",
            "langfuse.user.id": "user-1",
            "langfuse.trace.metadata": json.dumps({"parent_session_id": "ses-parent"}),
        }
    )
    try:
        with tracer.start_as_current_span(
            "chat glm",
            attributes={"gen_ai.conversation.id": "agentscope-state-id"},
        ) as span:
            assert span.attributes["gen_ai.conversation.id"] == "ses-parent"
            assert span.attributes["langfuse.session.id"] == "ses-parent"
            assert span.attributes["user.id"] == "user-1"
            metadata = json.loads(span.attributes["langfuse.trace.metadata"])
            assert metadata == {"parent_session_id": "ses-parent"}
    finally:
        reset_trace_labels(token)


def test_identity_from_principal_matches_row_identity() -> None:
    principal = Principal(
        user_id="seat-1",
        tenant_id="t1",
        workspace_ids=["w1"],
        active_workspace_id="w1",
    )
    from_row = Identity(user_id="seat-1", tenant_id="t1", workspace_id="w1")
    assert Identity.from_principal(principal) == from_row


def _host_ctx(session_id: str = "ses-1", **request_context: object) -> SimpleNamespace:
    return SimpleNamespace(
        identity=Identity(user_id="seat-1", tenant_id="t1", workspace_id="w1"),
        session_id=session_id,
        chat_id="chat-" + "ab" * 16,
        session_factory=None,
        request_context=request_context,
    )


def test_host_tracer_uses_public_user_id() -> None:
    token = principal_ctx.set(
        Principal(
            user_id="seat-1",
            tenant_id="t1",
            workspace_ids=["w1"],
            active_workspace_id="w1",
        )
    )
    try:
        labels = host_tracer(_host_ctx())._labels_for_chat()
        assert labels["user.id"] == "seat-1"
        assert labels["langfuse.user.id"] == "seat-1"
    finally:
        principal_ctx.reset(token)


def test_host_tracer_child_hangs_on_parent_session() -> None:
    ctx = _host_ctx("ses-child", parent_session_id="ses-main")
    labels = host_tracer(ctx)._labels_for_chat()
    assert labels["session.id"] == "ses-main"
    assert labels["langfuse.session.id"] == "ses-main"
    assert json.loads(labels["langfuse.trace.metadata"])["parent_session_id"] == (
        "ses-child"
    )


def test_host_tracer_falls_back_to_seat_id() -> None:
    labels = host_tracer(_host_ctx())._labels_for_chat()
    assert labels["user.id"] == "seat-1"
    assert labels["langfuse.user.id"] == "seat-1"


def test_ctx_fields_normalize_datasource_and_datasets() -> None:
    ctx = _host_ctx(datasource_id=" ds-1 ", dataset_names=[" a ", 1, " "])
    _, _, extra = _ctx_fields(ctx)
    assert extra == {
        "workspace_id": "w1",
        "datasource_id": "ds-1",
        "dataset_names": [" a ", "1"],
    }
    _, _, bare = _ctx_fields(_host_ctx(datasource_id="  ", dataset_names=[]))
    assert bare == {"workspace_id": "w1"}


def test_trace_labels_reach_a_reply_end_task_without_baggage() -> None:
    provider = TracerProvider()
    provider.add_span_processor(_HostSpanProcessor())
    tracer = provider.get_tracer("test")

    async def _child() -> None:
        with tracer.start_as_current_span("settlement") as span:
            assert span.attributes["langfuse.session.id"] == "ses-parent"
            assert span.attributes["gen_ai.conversation.id"] == "ses-parent"

    async def _run() -> None:
        token = bind_trace_labels(
            {
                "session.id": "ses-parent",
                "langfuse.session.id": "ses-parent",
                "gen_ai.conversation.id": "ses-parent",
            }
        )
        task = asyncio.create_task(_child())
        reset_trace_labels(token)
        await task

    asyncio.run(_run())


def test_processor_copies_full_metadata_and_cache_counts() -> None:
    provider = TracerProvider()
    provider.add_span_processor(_HostSpanProcessor())
    tracer = provider.get_tracer("test")
    metadata = json.dumps(
        {
            "datasource_id": "ds-1",
            "dataset_names": ["orders"],
            "parent_session_id": "ses-parent",
        }
    )
    ctx = baggage.set_baggage("langfuse.trace.metadata", metadata)
    ctx = baggage.set_baggage("session.id", "ses-parent", context=ctx)
    token = context.attach(ctx)
    try:
        with tracer.start_as_current_span(
            "chat model",
            attributes={
                "agentscope.usage.cache_input_tokens": 4,
                "agentscope.usage.cache_creation_input_tokens": 0,
            },
        ) as span:
            stamped = json.loads(span.attributes["langfuse.trace.metadata"])
            assert "chat_id" not in stamped
            assert stamped["datasource_id"] == "ds-1"
            assert stamped["dataset_names"] == ["orders"]
            assert stamped["parent_session_id"] == "ses-parent"
            assert span.attributes["gen_ai.conversation.id"] == "ses-parent"
        assert span.attributes["gen_ai.usage.cache_read.input_tokens"] == 4
        assert "gen_ai.usage.cache_creation.input_tokens" not in span.attributes
    finally:
        context.detach(token)


def test_processor_hook_failure_never_reaches_business_code(caplog) -> None:
    """A broken stamping hook must not propagate out of span start/end.

    The SDK calls processors without its own guard; a hook that touches
    renamed SDK internals would otherwise raise inside ``span.end()`` and
    break the reply stream. The base processor swallows it, warns once per
    hook, then downgrades repeats to debug.
    """

    import logging

    class _BrokenProcessor(_HostSpanProcessor):
        def on_start_extra(self, span, parent_context=None) -> None:
            raise RuntimeError("sdk internals changed")

        def on_end_extra(self, span) -> None:
            raise RuntimeError("sdk internals changed")

    reset_warn_once()
    provider = TracerProvider()
    provider.add_span_processor(_BrokenProcessor())
    tracer = provider.get_tracer("test")
    try:
        with caplog.at_level(logging.WARNING, logger="qwenpaw_data.tracing.export.spans"):
            # Two full spans: the second one proves repeats stay quiet.
            with tracer.start_as_current_span("invoke_agent qwenpaw-data"):
                pass
            with tracer.start_as_current_span("invoke_agent qwenpaw-data"):
                pass
    finally:
        reset_warn_once()
    warnings = [
        record
        for record in caplog.records
        if record.name == "qwenpaw_data.tracing.export.spans"
        and record.levelno == logging.WARNING
    ]
    assert len(warnings) == 2
    assert any("on_start" in record.getMessage() for record in warnings)
    assert any("on_end" in record.getMessage() for record in warnings)


def _end_invoke(attributes: dict, exc: BaseException):  # -> ReadableSpan
    provider = TracerProvider()
    exporter = InMemorySpanExporter()
    provider.add_span_processor(_HostSpanProcessor())
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    tracer = provider.get_tracer("test")
    with tracer.start_as_current_span(
        "invoke_agent qwenpaw-data", attributes=attributes
    ) as span:
        span.record_exception(exc)
        span.set_status(StatusCode.ERROR, str(exc))
    (finished,) = exporter.get_finished_spans()
    return finished


def test_clarification_pause_is_not_exported_as_error() -> None:
    exported = _end_invoke(
        {"agentscope.agent.external_execution_pending_tools": '["ask_user_question"]'},
        GeneratorExit(),
    )
    assert exported.status.status_code == StatusCode.OK
    assert [event.name for event in exported.events] == []


def test_stop_and_real_errors_stay_error() -> None:
    stopped = _end_invoke({}, GeneratorExit())
    assert stopped.status.status_code == StatusCode.ERROR
    failed = _end_invoke(
        {"agentscope.agent.external_execution_pending_tools": '["ask_user_question"]'},
        RuntimeError("schema rejected"),
    )
    assert failed.status.status_code == StatusCode.ERROR
    assert "exception" in [event.name for event in failed.events]


@pytest.mark.asyncio
async def test_stamp_tool_state_keeps_terminal_values_only() -> None:
    provider = TracerProvider()
    tracer = provider.get_tracer("test")
    with tracer.start_as_current_span("execute_tool") as span:
        await stamp_tool_state(SimpleNamespace(state="error"))
        assert span.attributes["langfuse.observation.metadata.tool_state"] == "error"
        assert span.attributes["langfuse.observation.level"] == "ERROR"
        assert span.status.status_code == StatusCode.ERROR
        assert span.status.description == "error"
        await stamp_tool_state(SimpleNamespace(state="running"))
        assert span.attributes["langfuse.observation.metadata.tool_state"] == "error"
        assert span.status.status_code == StatusCode.ERROR
    with tracer.start_as_current_span("execute_tool") as span:
        await stamp_tool_state(SimpleNamespace(state="success"))
        assert span.attributes["langfuse.observation.level"] == "DEFAULT"
        assert span.status.status_code == StatusCode.OK
    with tracer.start_as_current_span("execute_tool") as span:
        await stamp_tool_state(SimpleNamespace(state="denied"))
        assert span.status.status_code == StatusCode.ERROR
        assert span.status.description == "denied"


@pytest.mark.asyncio
async def test_stamp_tool_state_sends_tool_failed_score(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from qwenpaw_data.host.core.agent.middleware import tracing as middleware

    sent: list[tuple[str, float, str]] = []

    async def fake_send(name: str, value: float) -> None:
        sent.append((state, name, value))

    monkeypatch.setattr(middleware, "tracer_send_span_score", fake_send)
    tracer = TracerProvider().get_tracer("test")
    for state in ("success", "error", "interrupted", "denied"):
        with tracer.start_as_current_span("execute_tool"):
            await stamp_tool_state(SimpleNamespace(state=state))
    assert sent == [
        ("success", "tool_failed", 0.0),
        ("error", "tool_failed", 1.0),
        ("interrupted", "tool_failed", 1.0),
    ]


@pytest.mark.asyncio
async def test_tracing_middleware_forwards_on_model_call() -> None:
    mw = TracingMiddleware()
    assert mw.is_implemented("on_model_call")
    seen: list[str] = []

    async def next_handler(**_kwargs: object) -> str:
        seen.append("next")
        return "ok"

    async def fake(
        _agent: object,
        input_kwargs: dict,
        handler: object,
    ) -> object:
        seen.append("agentscope")
        return await handler(**input_kwargs)

    mw._tracing.on_model_call = fake
    result = await mw.on_model_call(SimpleNamespace(), {}, next_handler)
    assert result == "ok"
    assert seen == ["agentscope", "next"]


def _reply_middleware_with_captured_bound_stream():
    mw = TracingMiddleware()
    mw._tracer = ChatTracer(session=tracer_session("u1", "ses-1"))
    captured = []

    async def passthrough(_agent, input_kwargs, next_handler):
        stream = next_handler(**input_kwargs)
        captured.append(stream)
        async for item in stream:
            yield item

    mw._tracing.on_reply = passthrough
    return mw, captured


@pytest.mark.asyncio
async def test_reply_trace_context_does_not_escape_yield() -> None:
    provider = TracerProvider()
    provider.add_span_processor(_HostSpanProcessor())
    tracer = provider.get_tracer("test")
    mw, _ = _reply_middleware_with_captured_bound_stream()

    async def next_handler():
        for name in ("inside-1", "inside-2"):
            with tracer.start_as_current_span(name) as span:
                assert span.attributes["session.id"] == "ses-1"
            yield name

    stream = mw.on_reply(SimpleNamespace(), {}, next_handler)
    assert await anext(stream) == "inside-1"
    with tracer.start_as_current_span("outside") as span:
        assert "session.id" not in (span.attributes or {})
    assert await anext(stream) == "inside-2"
    await stream.aclose()


@pytest.mark.asyncio
async def test_reply_stream_runs_to_end_and_clears_trace_context(caplog) -> None:
    provider = TracerProvider()
    provider.add_span_processor(_HostSpanProcessor())
    tracer = provider.get_tracer("test")
    mw, _ = _reply_middleware_with_captured_bound_stream()

    async def next_handler():
        for name in ("inside-1", "inside-2"):
            with tracer.start_as_current_span(name) as span:
                assert span.attributes["session.id"] == "ses-1"
            yield name

    items = [item async for item in mw.on_reply(SimpleNamespace(), {}, next_handler)]
    assert items == ["inside-1", "inside-2"]
    with tracer.start_as_current_span("after") as span:
        assert "session.id" not in (span.attributes or {})
    assert baggage.get_baggage("session.id") is None
    assert "Failed to detach context" not in caplog.text


@pytest.mark.asyncio
async def test_reply_bound_stream_closes_in_another_task(caplog) -> None:
    mw, captured = _reply_middleware_with_captured_bound_stream()

    async def next_handler():
        yield "event"

    stream = mw.on_reply(SimpleNamespace(), {}, next_handler)
    assert await anext(stream) == "event"
    assert len(captured) == 1
    await asyncio.create_task(captured[0].aclose())
    await stream.aclose()
    assert "created in a different Context" not in caplog.text
    assert "Failed to detach context" not in caplog.text


@pytest.mark.asyncio
async def test_reply_stream_cross_task_close_with_agentscope(
    monkeypatch,
    caplog,
) -> None:
    from agentscope.middleware._tracing import _trace as agentscope_trace

    provider = TracerProvider()
    provider.add_span_processor(_HostSpanProcessor())
    tracer = provider.get_tracer("test")
    monkeypatch.setattr(agentscope_trace, "_check_tracing_enabled", lambda: True)
    monkeypatch.setattr(agentscope_trace, "_get_tracer", lambda: tracer)
    mw = TracingMiddleware()
    mw._tracer = ChatTracer(session=tracer_session("u1", "ses-1"))
    agent = SimpleNamespace(
        name="agent",
        state=SimpleNamespace(session_id="agentscope-session", reply_id=None),
    )

    async def next_handler(**_kwargs):
        with tracer.start_as_current_span("inside") as span:
            assert span.attributes["session.id"] == "ses-1"
        yield "event"

    stream = mw.on_reply(agent, {}, next_handler)
    assert await anext(stream) == "event"
    await asyncio.create_task(stream.aclose())
    assert "created in a different Context" not in caplog.text
    assert "Failed to detach context" not in caplog.text


def test_skill_name_ignores_other_tools() -> None:
    assert skill_name_from_arguments('{"skill": " writer "}') == "writer"
    assert skill_name_from_arguments('{"query": "x"}') is None
    assert skill_name_from_arguments("not-json") is None


def _end_tool_span(state: str | None, *, overwrite_ok: bool):  # -> ReadableSpan
    provider = TracerProvider()
    exporter = InMemorySpanExporter()
    provider.add_span_processor(_HostSpanProcessor())
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    tracer = provider.get_tracer("test")
    with tracer.start_as_current_span("execute_tool") as span:
        if state is not None:
            asyncio.run(stamp_tool_state(SimpleNamespace(state=state)))
        if overwrite_ok:
            # AgentScope closes every clean tool stream with set_status(OK),
            # and the OTEL SDK lets OK overwrite ERROR — never the reverse.
            span.set_status(StatusCode.OK)
    (finished,) = exporter.get_finished_spans()
    return finished


def test_failed_tool_state_survives_tracing_ok_overwrite() -> None:
    exported = _end_tool_span("error", overwrite_ok=True)
    assert exported.status.status_code == StatusCode.ERROR
    assert exported.status.description == "error"
    assert exported.attributes["langfuse.observation.level"] == "ERROR"


def test_successful_tool_state_stays_ok() -> None:
    exported = _end_tool_span("success", overwrite_ok=True)
    assert exported.status.status_code == StatusCode.OK


def test_tool_span_without_state_is_not_touched() -> None:
    exported = _end_tool_span(None, overwrite_ok=True)
    assert exported.status.status_code == StatusCode.OK


def test_debug_processor_logs_cache_after_copy(caplog) -> None:
    import logging

    provider = TracerProvider()
    provider.add_span_processor(_HostSpanProcessor())
    provider.add_span_processor(DebugSpanProcessor())
    tracer = provider.get_tracer("test")
    with caplog.at_level(logging.INFO):
        with tracer.start_as_current_span("followup") as span:
            span.set_attribute("agentscope.usage.cache_input_tokens", 4)
    assert "tracing debug span=followup" in caplog.text
    assert "gen_ai.usage.cache_read.input_tokens=4" in caplog.text
    assert "gen_ai.usage.cache_creation.input_tokens=-" in caplog.text


def _usage(source: ModelUsageSource, source_step: str = "") -> ModelUsageContext:
    return ModelUsageContext(
        source=source,
        identity=Identity(user_id="u1", tenant_id="t1", workspace_id="w1"),
        session_id="ses-1",
        chat_id="chat-1",
        source_step=source_step,
    )


@pytest.fixture
def in_memory_tracer(monkeypatch: pytest.MonkeyPatch):
    """Route generation spans to a fresh provider; yield (provider, exporter)."""
    from opentelemetry import trace as otel_trace

    provider = TracerProvider()
    exporter = InMemorySpanExporter()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    monkeypatch.setattr(
        otel_trace,
        "get_tracer",
        lambda *_a, **_k: provider.get_tracer("test"),
    )
    return provider, exporter


def test_attach_generation_span_records_structured_output(in_memory_tracer) -> None:
    _, exporter = in_memory_tracer

    class _Model:
        model = "qwen-test"

        async def generate_structured_output(  # type: ignore[no-untyped-def]
            self, messages, structured_model, **kwargs
        ):
            return SimpleNamespace(
                content={"ok": True},
                usage=SimpleNamespace(input_tokens=2, output_tokens=4),
            )

    model = attach_generation_span(_Model(), _usage(ModelUsageSource.FOLLOWUP))
    asyncio.run(model.generate_structured_output(["hello"], {"type": "object"}))
    assert len(exporter.get_finished_spans()) == 1
    span = exporter.get_finished_spans()[0]
    assert span.name == "followup"
    assert span.attributes["gen_ai.request.model"] == "qwen-test"
    assert span.attributes["gen_ai.usage.input_tokens"] == 2
    assert span.attributes["gen_ai.usage.output_tokens"] == 4


def test_attach_generation_span_uses_source_step(in_memory_tracer) -> None:
    _, exporter = in_memory_tracer

    class _Model:
        model = "qwen-test"

        async def generate_structured_output(  # type: ignore[no-untyped-def]
            self, messages, structured_model, **kwargs
        ):
            return SimpleNamespace(content={}, usage=None)

    model = attach_generation_span(
        _Model(),
        _usage(ModelUsageSource.COMPARE, "compare_recommendations"),
    )
    asyncio.run(model.generate_structured_output([], {}))
    assert exporter.get_finished_spans()[0].name == "compare_recommendations"


def test_source_step_kwarg_overrides_model_source(in_memory_tracer) -> None:
    _, exporter = in_memory_tracer

    class _Model:
        model = "qwen-test"

        async def generate_structured_output(  # type: ignore[no-untyped-def]
            self, messages, structured_model, **kwargs
        ):
            return SimpleNamespace(content={}, usage=None)

    model = attach_generation_span(_Model(), _usage(ModelUsageSource.SETTLEMENT))
    asyncio.run(
        model.generate_structured_output([], {}, source_step="settlement.detect")
    )
    assert exporter.get_finished_spans()[0].name == "settlement.detect"


def test_finish_generation_copies_biztrace_fields() -> None:
    provider = TracerProvider()
    tracer = provider.get_tracer("test")
    cases = (
        (
            "biztrace.segment_kind",
            {"kind": "probe", "reason": "look"},
            "segment_kind",
            "probe",
        ),
        (
            "biztrace.continuity_decision",
            {"continues": False, "reason": "new"},
            "continues",
            "false",
        ),
        (
            "biztrace.sql_result",
            {"output_summary": "empty", "failure_kind": "no_table"},
            "failure_kind",
            "no_table",
        ),
    )
    for name, content, key, expected in cases:
        with tracer.start_as_current_span(name) as span:
            gen = _Generation(span)
            response = SimpleNamespace(content=content, usage=None)
            gen.finish(response)
            attach_biztrace_metadata(gen, name, response)
            assert span.attributes[f"langfuse.observation.metadata.{key}"] == expected


def test_finish_generation_skips_null_failure_kind() -> None:
    provider = TracerProvider()
    tracer = provider.get_tracer("test")
    with tracer.start_as_current_span("biztrace.sql_result") as span:
        gen = _Generation(span)
        response = SimpleNamespace(
            content={"output_summary": "ok", "failure_kind": None},
            usage=None,
        )
        gen.finish(response)
        attach_biztrace_metadata(gen, "biztrace.sql_result", response)
        assert "langfuse.observation.metadata.failure_kind" not in span.attributes


def test_attach_generation_span_copies_segment_kind(in_memory_tracer) -> None:
    _, exporter = in_memory_tracer

    class _Model:
        model = "qwen-test"

        async def generate_structured_output(  # type: ignore[no-untyped-def]
            self, messages, structured_model, **kwargs
        ):
            return SimpleNamespace(
                content={"kind": "delivery", "reason": "done"},
                usage=None,
            )

    model = attach_generation_span(_Model(), _usage(ModelUsageSource.BIZTRACE))
    asyncio.run(
        model.generate_structured_output([], {}, source_step="biztrace.segment_kind")
    )
    assert exporter.get_finished_spans()[0].name == "biztrace.segment_kind"
    assert (
        exporter.get_finished_spans()[0].attributes[
            "langfuse.observation.metadata.segment_kind"
        ]
        == "delivery"
    )


def test_attach_generation_span_records_biztrace(in_memory_tracer) -> None:
    _, exporter = in_memory_tracer
    calls = 0

    class _Model:
        model = "qwen-test"

        async def generate_structured_output(  # type: ignore[no-untyped-def]
            self, messages, structured_model, **kwargs
        ):
            nonlocal calls
            calls += 1
            return SimpleNamespace(content={"ok": True}, usage=None)

    model = attach_generation_span(_Model(), _usage(ModelUsageSource.BIZTRACE))
    result = asyncio.run(model.generate_structured_output(["hello"], {}))
    assert calls == 1
    assert result.content == {"ok": True}
    assert [span.name for span in exporter.get_finished_spans()] == ["biztrace"]


def test_attach_generation_span_skips_inside_chat_span(in_memory_tracer) -> None:
    provider, exporter = in_memory_tracer
    calls = 0

    class _Model:
        model = "qwen-test"

        async def generate_structured_output(  # type: ignore[no-untyped-def]
            self, messages, structured_model, **kwargs
        ):
            nonlocal calls
            calls += 1
            return SimpleNamespace(content={}, usage=None)

    model = attach_generation_span(_Model(), _usage(ModelUsageSource.FOLLOWUP))

    async def _call() -> None:
        tracer = provider.get_tracer("test")
        with tracer.start_as_current_span(
            "chat qwen-test",
            attributes={"gen_ai.operation.name": "chat"},
        ):
            await model.generate_structured_output([], {})

    asyncio.run(_call())
    assert calls == 1
    assert [span.name for span in exporter.get_finished_spans()] == ["chat qwen-test"]


def test_attach_generation_span_marks_errors(in_memory_tracer) -> None:
    _, exporter = in_memory_tracer

    class _Model:
        model = "qwen-test"

        async def generate_structured_output(  # type: ignore[no-untyped-def]
            self, messages, structured_model, **kwargs
        ):
            raise RuntimeError("schema rejected")

    model = attach_generation_span(_Model(), _usage(ModelUsageSource.SETTLEMENT))
    with pytest.raises(RuntimeError, match="schema rejected"):
        asyncio.run(model.generate_structured_output([], {}))
    assert exporter.get_finished_spans()[0].name == "settlement"
    assert exporter.get_finished_spans()[0].status.status_code == StatusCode.ERROR


def test_build_model_wraps_structured_output(
    monkeypatch: pytest.MonkeyPatch, in_memory_tracer
) -> None:
    from agentscope.model import OpenAIChatModel

    _, exporter = in_memory_tracer
    calls: list[tuple] = []

    async def fake_generate(self, messages, structured_model, **kwargs):  # type: ignore[no-untyped-def]
        calls.append((messages, structured_model, kwargs))
        return SimpleNamespace(
            content={"answer": 1},
            usage=SimpleNamespace(input_tokens=3, output_tokens=7),
        )

    monkeypatch.setattr(OpenAIChatModel, "generate_structured_output", fake_generate)

    model = build_model(
        ActiveModel(
            provider_id="openai",
            model_id="gpt-4o",
            api_key="secret",
            base_url="https://models.example.test/v1",
            chat_model="OpenAIChatModel",
            name="GPT-4o",
        ),
        usage=_usage(ModelUsageSource.DATA_AGENT),
    )
    response = asyncio.run(
        model.generate_structured_output(["hi"], {"type": "object"}, temperature=0.1)
    )
    # Passthrough: the wrapped model receives the same arguments and its
    # response comes back untouched.
    assert calls == [(["hi"], {"type": "object"}, {"temperature": 0.1})]
    assert response.content == {"answer": 1}
    # Span behavior: one generation span named after the usage source step.
    spans = exporter.get_finished_spans()
    assert [span.name for span in spans] == ["data-agent"]
    span = spans[0]
    assert span.attributes["gen_ai.request.model"] == "gpt-4o"
    assert span.attributes["gen_ai.usage.input_tokens"] == 3
    assert span.attributes["gen_ai.usage.output_tokens"] == 7


def _enable_score_env(monkeypatch) -> None:
    monkeypatch.setenv("TRACING_ENABLED", "1")
    monkeypatch.setenv("TRACING_HOST", "https://langfuse.example")
    monkeypatch.setenv("TRACING_PUBLIC_KEY", "pk")
    monkeypatch.setenv("TRACING_SECRET_KEY", "sk")


def _delivery_row(row_id: str, *, chat_id: str, attempts: int = 0) -> SimpleNamespace:
    return SimpleNamespace(
        id=row_id,
        chat_id=chat_id,
        session_id=None,
        name="copy",
        data_type="BOOLEAN",
        value_num=1,
        value_text=None,
        comment=None,
        metadata_json=None,
        op="create",
        sent_at=None,
        attempts=attempts,
    )


def _score_harness(
    monkeypatch,
    rows: list,
    posted: list[dict],
) -> object:
    """Shared fakes for flush_scores: db returning ``rows``, http recording posts."""

    from qwenpaw_data.host.core.tracing import scores as scores_mod

    class _Db:
        async def scalars(self, _stmt):
            return SimpleNamespace(all=lambda: list(rows))

        async def scalar(self, _stmt):
            raise AssertionError("scores must not query the db for trace ids")

        async def get(self, _model, row_id):
            return next((row for row in rows if row.id == row_id), None)

        def expunge(self, _row) -> None:
            return None

        async def commit(self):
            return None

    class _SessionFactory:
        async def __aenter__(self):
            return _Db()

        async def __aexit__(self, *exc):
            return False

        def __call__(self):
            return self

    class _Resp:
        status_code = 200

        def raise_for_status(self):
            return None

    class _Client:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def post(self, _url, json=None, auth=None):
            posted.append(json)
            return _Resp()

    _enable_score_env(monkeypatch)
    monkeypatch.setattr(scores_mod.httpx, "AsyncClient", _Client)
    return _SessionFactory()


async def test_flush_scores_poison_row_does_not_starve_queue(
    monkeypatch,
) -> None:
    """推导不出 trace id 的老 chat 行立即死信，不让后面的行饿死。"""

    poison = _delivery_row("poison", chat_id="chat-no-trace")
    good = _delivery_row("good", chat_id="chat-good")
    # good 带 session_id 走 session 分支；poison 的老 chat id 推导不出 trace id。
    good.session_id = "ses-good"
    posted: list[dict] = []

    factory = _score_harness(monkeypatch, [poison, good], posted)

    # 一次 flush：poison 立即死信（不占 MAX 轮队头），good 正常发出。
    await flush_scores(factory)
    assert [body["id"] for body in posted] == ["good"]
    assert poison.attempts >= MAX_DELIVERY_ATTEMPTS
    assert poison.sent_at is not None


async def test_flush_scores_derives_trace_id_from_chat_id(monkeypatch) -> None:
    """新 chat id 的 score 直接从 chat id 推导 traceId 发出，不查 DB。"""

    chat_id = "chat-f33fa5fa9c8b4cb28ae4055f394f3ee5"
    row = _delivery_row("r1", chat_id=chat_id)
    posted: list[dict] = []

    factory = _score_harness(monkeypatch, [row], posted)

    await flush_scores(factory)
    assert [body["traceId"] for body in posted] == [chat_id[5:]]
    assert row.sent_at is not None


async def test_flush_scores_replace_deletes_then_posts(monkeypatch) -> None:
    """已发出的 score 改值后走 delete + POST，避免 Langfuse 拒绝同 id 二次创建。"""

    from qwenpaw_data.host.core.tracing import scores as scores_mod

    chat_id = "chat-f33fa5fa9c8b4cb28ae4055f394f3ee5"
    row = _delivery_row("fbk-1", chat_id=chat_id)
    row.op = "replace"
    row.name = "reaction"
    row.data_type = "CATEGORICAL"
    row.value_num = None
    row.value_text = "dislike"
    deleted: list[str] = []
    posted: list[dict] = []

    class _Resp:
        status_code = 204

        def raise_for_status(self):
            return None

    class _Client:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def delete(self, url, auth=None):
            deleted.append(url)
            return _Resp()

        async def post(self, _url, json=None, auth=None):
            posted.append(json)
            return _Resp()

    _enable_score_env(monkeypatch)
    monkeypatch.setattr(scores_mod.httpx, "AsyncClient", _Client)

    class _Db:
        async def scalars(self, _stmt):
            return SimpleNamespace(all=lambda: [row])

        async def get(self, _model, row_id):
            return row if row.id == row_id else None

        def expunge(self, _row) -> None:
            return None

        async def commit(self):
            return None

    class _Factory:
        async def __aenter__(self):
            return _Db()

        async def __aexit__(self, *exc):
            return False

        def __call__(self):
            return self

    await flush_scores(_Factory())
    assert deleted == ["https://langfuse.example/api/public/scores/fbk-1"]
    assert posted[0]["value"] == "dislike"
    assert row.op == "create"
    assert row.sent_at is not None


class _SpanIdRow:
    otel_span_id = "0123456789abcdef"


def _chat_row_factory(row: object | None) -> object:
    class _Db:
        async def get(self, _model, _id):
            return row

    class _SessionFactory:
        async def __aenter__(self):
            return _Db()

        async def __aexit__(self, *exc):
            return False

        def __call__(self):
            return self

    return _SessionFactory()


async def test_attach_saved_chat_trace_derives_trace_id_from_chat_id() -> None:
    """续 trace：trace id 从 chat id 推导，span id 从 DB 读。"""
    from opentelemetry import trace as otel_trace

    chat_id = "chat-f33fa5fa9c8b4cb28ae4055f394f3ee5"
    token = await attach_saved_chat_trace(_chat_row_factory(_SpanIdRow()), chat_id)
    assert token is not None
    try:
        parent = otel_trace.get_current_span().get_span_context()
        assert format(parent.trace_id, "032x") == chat_id[5:]
        assert format(parent.span_id, "016x") == "0123456789abcdef"
    finally:
        context.detach(token)


async def test_attach_saved_chat_trace_skips_legacy_chat_id() -> None:
    """老 chat id（带横线）推导不出 trace id，不续也不查 DB。"""
    chat_id = "chat-f33fa5fa-9c8b-4cb2-8ae4-055f394f3ee5"
    token = await attach_saved_chat_trace(_chat_row_factory(_SpanIdRow()), chat_id)
    assert token is None


async def test_attach_saved_chat_trace_skips_when_no_span_id() -> None:
    chat_id = "chat-f33fa5fa9c8b4cb28ae4055f394f3ee5"
    token = await attach_saved_chat_trace(
        _chat_row_factory(SimpleNamespace(otel_span_id=None)), chat_id
    )
    assert token is None


async def test_biztrace_pipeline_opens_a_detached_root_span(
    monkeypatch, tmp_path
) -> None:
    from qwenpaw_data.host.core.algo.biztrace import pipeline as pipeline_mod
    from qwenpaw_data.host.core.algo.biztrace.presentation import PresentationBuilder
    from qwenpaw_data.host.core.algo.biztrace.settings import BizTraceSettings
    from qwenpaw_data.host.core.algo.biztrace.store import BizTraceStore, build_store_paths

    provider = TracerProvider(id_generator=ChatTraceIdGenerator())
    provider.add_span_processor(_HostSpanProcessor())
    exporter = InMemorySpanExporter()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    from qwenpaw_data.tracing.core.names import Tracer

    tracer = provider.get_tracer(Tracer.HOST)
    monkeypatch.setattr(
        "opentelemetry.trace.get_tracer",
        lambda *_a, **_k: tracer,
    )

    chat_id = "chat-f33fa5fa9c8b4cb28ae4055f394f3ee5"
    source_trace_id = chat_id[5:]
    chat_tracer = ChatTracer(
        tracer_session("u1", "ses-1"),
        chat_id=chat_id,
        step="biztrace",
    )
    pipeline = pipeline_mod.BizTracePipeline(
        session_id="ses-1",
        store=BizTraceStore(
            build_store_paths(session_id="ses-1", log_dir=str(tmp_path))
        ),
        presenter=PresentationBuilder(),
        settings=BizTraceSettings(trace2segment_enabled=False),
        tracer=chat_tracer,
    )
    chat_token = bind_chat_trace(chat_id)
    label_token = bind_trace_labels(
        {"langfuse.trace.name": "chat", "session.id": "ses-reply"}
    )
    try:
        pipeline.start()
        # start() restores the caller's context: reply spans stay on the chat trace.
        with tracer.start_as_current_span("invoke_agent qwenpaw-data") as reply_span:
            assert format(reply_span.get_span_context().trace_id, "032x") == (
                source_trace_id
            )
        await pipeline.aclose()
    finally:
        reset_trace_labels(label_token)
        reset_chat_trace(chat_token)

    by_name = {span.name: span for span in exporter.get_finished_spans()}
    biztrace = by_name["biztrace"]
    assert biztrace.parent is None
    biz_trace_id = format(biztrace.context.trace_id, "032x")
    assert biz_trace_id != source_trace_id
    assert biztrace.attributes["session.id"] == "ses-1"
    assert biztrace.attributes["langfuse.trace.name"] == "biztrace"
    metadata = json.loads(biztrace.attributes["langfuse.trace.metadata"])
    assert metadata == {"source_trace_id": source_trace_id}
    reply = by_name["invoke_agent qwenpaw-data"]
    assert format(reply.context.trace_id, "032x") == source_trace_id


def _cm_client(name: str | None = None) -> SimpleNamespace:
    from qwenpaw_data.host.core.settings.cm import CmSettings

    calls: list[dict[str, str]] = []

    async def set_runtime_headers(headers: dict[str, str]) -> None:
        calls.append(dict(headers))

    return SimpleNamespace(
        name=name if name is not None else CmSettings.MCP_NAME,
        _runtime_headers={},
        set_runtime_headers=set_runtime_headers,
        calls=calls,
    )


def _ctx_with_mcps(clients: list) -> SimpleNamespace:
    async def list_mcps(*, agent_id: str, session_id: str) -> list:
        return clients

    return SimpleNamespace(
        workspace=SimpleNamespace(list_mcps=list_mcps),
        paths=SimpleNamespace(agent_id="a1"),
        session_id="ses_1",
    )


def _cm_tracer(ctx: SimpleNamespace) -> tuple:
    """(run_context, injection cache) for ``_inject_cm_traceparent`` calls."""
    return ctx, {}


async def test_inject_cm_traceparent_skips_unchanged_clients() -> None:
    cm = _cm_client()
    other = _cm_client(name="other-mcp")
    ctx, cache = _cm_tracer(_ctx_with_mcps([cm, other]))
    otel_tracer = TracerProvider().get_tracer("test")
    with otel_tracer.start_as_current_span("root"):
        await _inject_cm_traceparent(ctx, cache)
        await _inject_cm_traceparent(ctx, cache)
    # Same carrier twice: the CM client is rewritten once, the foreign client
    # never.
    assert len(cm.calls) == 1
    assert cm.calls[0]["traceparent"].startswith("00-")
    assert other.calls == []


async def test_inject_cm_traceparent_reinjects_on_new_trace() -> None:
    cm = _cm_client()
    ctx, cache = _cm_tracer(_ctx_with_mcps([cm]))
    otel_tracer = TracerProvider().get_tracer("test")
    with otel_tracer.start_as_current_span("one"):
        await _inject_cm_traceparent(ctx, cache)
    with otel_tracer.start_as_current_span("two"):
        await _inject_cm_traceparent(ctx, cache)
    assert len(cm.calls) == 2
    assert cm.calls[0]["traceparent"] != cm.calls[1]["traceparent"]


async def test_inject_cm_traceparent_drops_removed_clients() -> None:
    cm = _cm_client()
    ctx = _ctx_with_mcps([cm])
    ctx, cache = _cm_tracer(ctx)
    otel_tracer = TracerProvider().get_tracer("test")
    with otel_tracer.start_as_current_span("root"):
        await _inject_cm_traceparent(ctx, cache)
        assert id(cm) in cache
        ctx.workspace = SimpleNamespace(
            list_mcps=_ctx_with_mcps([]).workspace.list_mcps
        )
        await _inject_cm_traceparent(ctx, cache)
    assert cache == {}


async def test_record_behavior_never_raises() -> None:
    """行为落库是附属功能：DB 挂了也只记日志，返回空 id。"""

    class _Db:
        def add(self, _row: object) -> None:
            raise RuntimeError("db gone")

    event_id = await tracer_record_behavior(
        _Db(),
        Identity(user_id="u", tenant_id="t", workspace_id="w"),
        session_id="ses-1",
        chat_id="chat-1",
        kind="trace_view",
    )
    assert event_id == ""


async def test_record_behavior_writes_event_and_score() -> None:
    added: list[object] = []

    class _Db:
        def add(self, row: object) -> None:
            added.append(row)

        async def get(self, *_args: object) -> None:
            return None

        async def flush(self) -> None:
            return None

    event_id = await tracer_record_behavior(
        _Db(),
        Identity(user_id="u", tenant_id="t", workspace_id="w"),
        session_id="ses-1",
        chat_id="chat-1",
        kind="share",
        score_name="share",
        value="workspace",
    )
    assert event_id.startswith("bev-")
    assert len(added) == 2  # behavior event + score delivery
