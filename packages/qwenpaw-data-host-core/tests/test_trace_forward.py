# -*- coding: utf-8 -*-
from __future__ import annotations

import asyncio
from typing import Any, AsyncGenerator

import pytest

from pathlib import Path

from agentscope.event import ExternalExecutionResultEvent, ReplyEndEvent
from agentscope.message import UserMsg

from qwenpaw_data.host.core.agent.middleware import trace_forward
from qwenpaw_data.host.core.agent.middleware.trace_forward import TraceForwardMiddleware
from qwenpaw_data.host.core.algo.biztrace.transformer import BizTraceTransformer
from qwenpaw_data.host.core.db.engine import get_session_factory
from qwenpaw_data.host.core.domain.identity import Identity
from qwenpaw_data.host.core.domain.preference import ActiveModel, UserRuntimeConfig
from qwenpaw_data.host.core.runtime.context import RunContext
from qwenpaw_data.host.core.settings import clear_settings, get_settings


class _FakeEvent:
    def __init__(self, payload: dict[str, Any]) -> None:
        self._payload = payload

    def model_dump(self) -> dict[str, Any]:
        return dict(self._payload)


def _reply_end() -> ReplyEndEvent:
    return ReplyEndEvent(session_id="ses_1", reply_id="rpl_1")


class _RecordingTransformer:
    """替身 Transformer：只记录 start/append/join 的调用顺序。"""

    def __init__(self, **kwargs: Any) -> None:
        self.init_kwargs = kwargs
        self.chat_id = kwargs["run_context"].chat_id
        self.calls: list[tuple[str, Any]] = []

    async def start(self) -> None:
        self.calls.append(("start", None))

    async def append(self, entry: Any, *, last: bool = False) -> None:
        self.calls.append(("append", (entry, last)))

    async def join(self) -> None:
        self.calls.append(("join", None))

    def eof_count(self) -> int:
        return sum(1 for name, args in self.calls if name == "append" and args[1])


def _make_run_context(*, workspace_dir: str = "/tmp/ws") -> RunContext:
    from qwenpaw_data.host.core.paths import UserAgentPaths

    runtime = get_settings().runtime
    model = ActiveModel(
        provider_id="openai",
        model_id="m1",
        api_key="sk-test",
        base_url="https://example.invalid/v1",
        chat_model="test-model",
        name="test-model",
    )
    return RunContext(
        session_id="ses_1",
        chat_id="chat_1",
        workspace=object(),
        paths=UserAgentPaths(
            home=Path(workspace_dir),
            tenant_id="t1",
            workspace_id="w1",
            user_id="u1",
            agent_id="a1",
            session_id="ses_1",
        ),
        user_runtime_config=UserRuntimeConfig(
            default=model,
            light=None,
            react_max_iters=10,
            llm_retry_enabled=False,
            llm_max_retries=0,
            context_trigger_ratio=runtime.context_trigger_ratio,
            context_reserve_ratio=runtime.context_reserve_ratio,
            tool_result_limit=runtime.tool_result_limit,
            tool_timeout_enabled=runtime.tool_timeout_enabled,
            tool_timeout_seconds=runtime.tool_timeout_seconds,
        ),
        request_context={"datasource_id": "ds_1"},
        identity=Identity(user_id="u1", tenant_id="t1", workspace_id="w1"),
        session_factory=get_session_factory(),
    )


def _make_middleware(
    monkeypatch,
) -> tuple[TraceForwardMiddleware, _RecordingTransformer]:
    monkeypatch.setattr(trace_forward, "BizTraceTransformer", _RecordingTransformer)
    mw = TraceForwardMiddleware(
        run_context=_make_run_context(),
        envelope=object(),
    )
    return mw, mw._transformer


def _handler(events: list[Any]):
    async def next_handler() -> AsyncGenerator:
        for ev in events:
            yield ev

    return next_handler


async def _drive(
    mw: TraceForwardMiddleware,
    events: list[Any],
    *,
    inputs: Any | None = None,
) -> list[Any]:
    seen = []
    async for ev in mw.on_reply(
        agent=None,
        input_kwargs={"inputs": inputs or UserMsg(name="user", content="hi")},
        next_handler=_handler(events),
    ):
        seen.append(ev)
    return seen


@pytest.mark.asyncio
async def test_normal_reply_calls_start_appends_eof_then_join(monkeypatch) -> None:
    mw, tr = _make_middleware(monkeypatch)
    deltas = [
        _FakeEvent({"type": "TEXT_BLOCK_DELTA", "delta": str(i)}) for i in range(3)
    ]
    events = [*deltas, _reply_end()]

    seen = await _drive(mw, events)

    assert seen == events
    assert tr.calls[0] == ("start", None)
    entry, last = tr.calls[1][1]
    assert entry["kind"] == "user_input" and last is False
    for i, (name, (entry, last)) in enumerate(tr.calls[2:5]):
        assert name == "append" and not last
        assert entry == {
            "kind": "agent_event",
            "payload": {"type": "TEXT_BLOCK_DELTA", "delta": str(i)},
        }
    name, (entry, last) = tr.calls[5]
    assert name == "append" and entry["kind"] == "agent_event" and not last
    assert tr.calls[6] == ("append", (None, True))
    assert tr.calls[7] == ("join", None)
    assert len(tr.calls) == 8


@pytest.mark.asyncio
async def test_pause_then_resume_sends_exactly_one_eof(monkeypatch) -> None:
    """ask_user_question 暂停被 aclose 不发 EOF，恢复流结束才收尾。"""
    mw, tr = _make_middleware(monkeypatch)

    gen = mw.on_reply(
        agent=None,
        input_kwargs={"inputs": UserMsg(name="user", content="hi")},
        next_handler=_handler(
            [_FakeEvent({"type": "TOOL_CALL_START", "tool": "ask_user_question"})]
        ),
    )
    async for _ in gen:
        break  # 模拟 executor 收到 RequireExternalExecutionEvent 后中断
    await gen.aclose()

    assert tr.eof_count() == 0
    assert ("join", None) not in tr.calls

    # 恢复：executor 用 ExternalExecutionResultEvent 开新 reply_stream
    resume_events = [
        _FakeEvent({"type": "TOOL_RESULT_END", "status": "answered"}),
        _reply_end(),
    ]
    seen = await _drive(
        mw,
        resume_events,
        inputs=ExternalExecutionResultEvent(reply_id="rpl_1", execution_results=[]),
    )

    assert seen == resume_events
    assert tr.eof_count() == 1
    assert tr.calls[-2] == ("append", (None, True))
    assert tr.calls[-1] == ("join", None)


@pytest.mark.asyncio
async def test_non_msg_inputs_are_not_fed_as_user_input(monkeypatch) -> None:
    mw, tr = _make_middleware(monkeypatch)

    await _drive(
        mw,
        [_reply_end()],
        inputs=ExternalExecutionResultEvent(reply_id="rpl_1", execution_results=[]),
    )

    kinds = [
        args[0]["kind"]
        for name, args in tr.calls
        if name == "append" and args[0] is not None
    ]
    assert "user_input" not in kinds


@pytest.mark.asyncio
async def test_stream_error_schedules_one_tracked_abort_close(monkeypatch) -> None:
    """异常中断的流由 middleware 的受管 abort 任务收尾；并发重复 abort 复用同一任务。"""
    release = asyncio.Event()

    class _SlowJoinTransformer(_RecordingTransformer):
        async def join(self) -> None:
            await release.wait()
            await super().join()

    monkeypatch.setattr(trace_forward, "BizTraceTransformer", _SlowJoinTransformer)
    mw = TraceForwardMiddleware(
        run_context=_make_run_context(),
        envelope=object(),
    )
    tr = mw._transformer

    async def broken_handler() -> AsyncGenerator:
        yield _FakeEvent({"type": "TEXT_BLOCK_START"})
        raise RuntimeError("agent exploded")

    with pytest.raises(RuntimeError, match="agent exploded"):
        async for _ in mw.on_reply(
            agent=None,
            input_kwargs={"inputs": UserMsg(name="user", content="hi")},
            next_handler=lambda: broken_handler(),
        ):
            pass

    task = mw._abort_task
    assert task is not None

    # 再次 abort 复用挂着的任务，不叠加新任务。
    await asyncio.sleep(0)
    mw._schedule_abort_close()
    assert mw._abort_task is task

    release.set()
    await task
    assert tr.eof_count() == 1
    assert ("join", None) in tr.calls

    # 非流式 stop 时 end_stream 补发 EOF；重复 EOF 由 transformer 幂等 flush 兜底。
    await mw.end_stream()
    assert tr.eof_count() == 2


@pytest.mark.asyncio
async def test_abort_close_failure_is_retrieved_not_leaked(monkeypatch) -> None:
    """abort 任务内部失败被 _close_after_abort 吞掉，done 回调静默收尾。"""

    class _FailingTransformer(_RecordingTransformer):
        async def append(self, entry: Any, *, last: bool = False) -> None:
            if last:
                raise RuntimeError("transformer gone")
            await super().append(entry, last=last)

    monkeypatch.setattr(trace_forward, "BizTraceTransformer", _FailingTransformer)
    mw = TraceForwardMiddleware(
        run_context=_make_run_context(),
        envelope=object(),
    )

    async def broken_handler() -> AsyncGenerator:
        raise RuntimeError("agent exploded")
        yield

    with pytest.raises(RuntimeError, match="agent exploded"):
        async for _ in mw.on_reply(
            agent=None,
            input_kwargs={"inputs": UserMsg(name="user", content="hi")},
            next_handler=lambda: broken_handler(),
        ):
            pass

    assert mw._abort_task is not None
    await mw._abort_task  # 不抛异常：失败已被记录并吞掉
    assert mw._abort_task.exception() is None


@pytest.mark.asyncio
async def test_transformer_flushes_pipeline_exactly_once() -> None:
    """重复发结束标记是安全的：真实 Transformer 的 flush 任务只会创建一次。"""

    class _CountingPipeline:
        def __init__(self) -> None:
            self.aclose_calls = 0

        def on_trace_event(self, entry: dict[str, Any]) -> None:
            pass

        async def aclose(self) -> None:
            self.aclose_calls += 1

    transformer = BizTraceTransformer(
        run_context=_make_run_context(), envelope=object()
    )
    pipeline = _CountingPipeline()
    transformer._pipeline = pipeline

    await transformer.append(None, last=True)
    await transformer.join()
    await transformer.append(None, last=True)
    await transformer.join()

    assert pipeline.aclose_calls == 1


@pytest.mark.asyncio
async def test_join_timeout_is_owned_by_middleware(monkeypatch) -> None:
    class _HangingTransformer(_RecordingTransformer):
        async def join(self) -> None:
            await asyncio.Event().wait()

    monkeypatch.setenv("QWENPAW_DATA_RUNTIME_TRANSFORMER_JOIN_TIMEOUT_S", "0.05")
    clear_settings()
    monkeypatch.setattr(trace_forward, "BizTraceTransformer", _HangingTransformer)
    try:
        mw = TraceForwardMiddleware(
            run_context=_make_run_context(),
            envelope=object(),
        )
        end = _reply_end()
        assert await _drive(mw, [end]) == [end]
    finally:
        clear_settings()
