# -*- coding: utf-8 -*-
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any, AsyncGenerator

import pytest
from agentscope.event import ReplyEndEvent
from agentscope.message import UserMsg
from qwenpaw_data.host.core.agent.middleware.settlement import (
    ConfirmedSettlementPromptMiddleware,
    SettlementDetectionMiddleware,
)
from qwenpaw_data.host.core.db.engine import (
    TEST_TENANT_ID,
    TEST_USER_ID,
    TEST_WORKSPACE_ID,
    get_session_factory,
    init_db,
    reset_engine,
)
from qwenpaw_data.host.core.domain.identity import Identity
from qwenpaw_data.host.core.domain.session import Session
from qwenpaw_data.host.core.repositories import SessionRepository, SettlementRepository
from qwenpaw_data.host.core.runtime.context import RunContext
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter


class _FakeDb:
    def __init__(self) -> None:
        self.committed = False

    async def commit(self) -> None:
        self.committed = True


class _FakeSessionFactory:
    def __init__(self, db: _FakeDb) -> None:
        self._db = db

    def __call__(self) -> Any:
        factory = self

        class _Ctx:
            async def __aenter__(self) -> _FakeDb:
                return factory._db

            async def __aexit__(self, *args: Any) -> None:
                return None

        return _Ctx()


class _FakeStream:
    def __init__(self, identity: Identity) -> None:
        self.identity = identity


class _FakeEnvelope:
    def __init__(self, session_factory: Any, identity: Identity) -> None:
        self._session_factory = session_factory
        self.stream = _FakeStream(identity)


def _identity() -> Identity:
    return Identity(user_id="u1", tenant_id="t1", workspace_id="w1")


def _run_context(chat_id: str = "chat_1") -> RunContext:
    from qwenpaw_data.host.core.paths import UserAgentPaths

    return RunContext(
        session_id="ses_1",
        chat_id=chat_id,
        workspace=SimpleNamespace(),
        paths=UserAgentPaths(
            home=Path("."),
            tenant_id="t1",
            workspace_id="w1",
            user_id="u1",
            agent_id="a1",
            session_id="ses_1",
        ),
        user_runtime_config=SimpleNamespace(),  # type: ignore[arg-type]
        identity=_identity(),
        session_factory=get_session_factory(),
    )


def _reply_end() -> ReplyEndEvent:
    return ReplyEndEvent(session_id="ses_1", reply_id="rpl_1")


def _handler(events: list[Any]):
    async def next_handler() -> AsyncGenerator:
        for ev in events:
            yield ev

    return next_handler


def _make_middleware() -> SettlementDetectionMiddleware:
    return SettlementDetectionMiddleware(
        run_context=_run_context(),
        envelope=_FakeEnvelope(_FakeSessionFactory(_FakeDb()), _identity()),
    )


async def test_run_calls_settlement_manager_then_commits(monkeypatch) -> None:
    db = _FakeDb()
    identity = _identity()
    envelope = _FakeEnvelope(_FakeSessionFactory(db), identity)
    mw = SettlementDetectionMiddleware(run_context=_run_context(), envelope=envelope)

    calls: list[tuple[str, Any]] = []

    class FakeManager:
        def __init__(self, session: Any, *, identity: Identity) -> None:
            calls.append(("init", (session, identity)))

        async def on_chat_finish(self, *, chat_id: str, session_id: str) -> None:
            calls.append(("on_chat_finish", (chat_id, session_id)))

    monkeypatch.setitem(
        sys.modules,
        "qwenpaw_data.host.core.algo.settlement",
        SimpleNamespace(SettlementManager=FakeManager),
    )

    await mw._run()

    assert calls == [
        ("init", (db, identity)),
        ("on_chat_finish", ("chat_1", "ses_1")),
    ]
    assert db.committed is True


async def test_reply_end_schedules_settlement(monkeypatch) -> None:
    mw = _make_middleware()
    started = asyncio.Event()

    async def _mark() -> None:
        started.set()

    monkeypatch.setattr(mw, "_run", _mark)

    seen: list[Any] = []
    async for ev in mw.on_reply(
        agent=None,
        input_kwargs={"inputs": UserMsg(name="user", content="hi")},
        next_handler=_handler([_reply_end()]),
    ):
        seen.append(ev)

    assert len(seen) == 1
    await asyncio.wait_for(started.wait(), timeout=1)


async def test_pause_without_reply_end_does_not_run(monkeypatch) -> None:
    """ask_user_question pause acloses without ReplyEndEvent — no detection."""
    mw = _make_middleware()
    runs: list[str] = []

    async def _track() -> None:
        runs.append("run")

    monkeypatch.setattr(mw, "_run", _track)

    gen = mw.on_reply(
        agent=None,
        input_kwargs={"inputs": UserMsg(name="user", content="hi")},
        next_handler=_handler(
            [SimpleNamespace(type="TOOL_CALL_START", tool="ask_user_question")]
        ),
    )
    async for _ in gen:
        break
    await gen.aclose()

    assert runs == []


async def test_resume_reply_end_runs_once(monkeypatch) -> None:
    mw = _make_middleware()
    runs: list[str] = []
    done = asyncio.Event()

    async def _track() -> None:
        runs.append("run")
        done.set()

    monkeypatch.setattr(mw, "_run", _track)

    gen = mw.on_reply(
        agent=None,
        input_kwargs={"inputs": UserMsg(name="user", content="hi")},
        next_handler=_handler(
            [SimpleNamespace(type="TOOL_CALL_START", tool="ask_user_question")]
        ),
    )
    async for _ in gen:
        break
    await gen.aclose()
    assert runs == []

    async for _ in mw.on_reply(
        agent=None,
        input_kwargs={"inputs": UserMsg(name="user", content="answer")},
        next_handler=_handler([_reply_end()]),
    ):
        pass

    await asyncio.wait_for(done.wait(), timeout=1)
    assert runs == ["run"]


def _tracing_provider():
    from qwenpaw_data.host.core.tracing.adapters import _HostSpanProcessor
    from qwenpaw_data.tracing.runtime.host.chat_id import ChatTraceIdGenerator
    from opentelemetry.sdk.trace import TracerProvider

    provider = TracerProvider(id_generator=ChatTraceIdGenerator())
    provider.add_span_processor(_HostSpanProcessor())
    exporter = InMemorySpanExporter()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    return provider, exporter


async def test_settlement_runs_on_a_detached_trace(monkeypatch) -> None:
    from qwenpaw_data.tracing.runtime.host.chat_id import (
        bind_chat_trace,
        reset_chat_trace,
        trace_id_from_chat_id,
    )

    provider, exporter = _tracing_provider()
    from qwenpaw_data.tracing.core.names import Tracer

    tracer = provider.get_tracer(Tracer.HOST)
    monkeypatch.setattr(
        "opentelemetry.trace.get_tracer",
        lambda *_a, **_k: tracer,
    )

    chat_id = "chat-f33fa5fa9c8b4cb28ae4055f394f3ee5"
    identity = _identity()
    mw = SettlementDetectionMiddleware(
        run_context=_run_context(chat_id),
        envelope=_FakeEnvelope(_FakeSessionFactory(_FakeDb()), identity),
    )

    class FakeManager:
        def __init__(self, session: Any, *, identity: Identity) -> None:
            pass

        async def on_chat_finish(self, *, chat_id: str, session_id: str) -> None:
            pass

    monkeypatch.setitem(
        sys.modules,
        "qwenpaw_data.host.core.algo.settlement",
        SimpleNamespace(SettlementManager=FakeManager),
    )

    source_trace_id = trace_id_from_chat_id(chat_id)
    assert source_trace_id is not None
    token = bind_chat_trace(chat_id)
    try:
        with tracer.start_as_current_span("invoke_agent qwenpaw-data"):
            await mw._run()
    finally:
        reset_chat_trace(token)

    by_name = {span.name: span for span in exporter.get_finished_spans()}
    settlement = by_name["settlement"]
    assert settlement.parent is None
    assert format(settlement.context.trace_id, "032x") != source_trace_id
    assert settlement.attributes["langfuse.trace.name"] == "settlement"
    assert list(settlement.attributes["langfuse.trace.tags"]) == ["settlement"]
    assert settlement.attributes["session.id"] == "ses_1"
    assert settlement.attributes["user.id"] == "u1"
    metadata = json.loads(settlement.attributes["langfuse.trace.metadata"])
    assert metadata["source_trace_id"] == source_trace_id
    reply = by_name["invoke_agent qwenpaw-data"]
    assert format(reply.context.trace_id, "032x") == source_trace_id


async def test_skill_evolution_runs_on_a_detached_trace(monkeypatch) -> None:
    from qwenpaw_data.host.core.agent.middleware.settlement import (
        SkillEvolutionDetectionMiddleware,
    )
    from qwenpaw_data.tracing.runtime.host.chat_id import (
        bind_chat_trace,
        reset_chat_trace,
        trace_id_from_chat_id,
    )

    provider, exporter = _tracing_provider()
    from qwenpaw_data.tracing.core.names import Tracer

    tracer = provider.get_tracer(Tracer.HOST)
    monkeypatch.setattr(
        "opentelemetry.trace.get_tracer",
        lambda *_a, **_k: tracer,
    )

    chat_id = "chat-f33fa5fa9c8b4cb28ae4055f394f3ee5"
    identity = _identity()
    mw = SkillEvolutionDetectionMiddleware(
        run_context=_run_context(chat_id),
        envelope=_FakeEnvelope(_FakeSessionFactory(_FakeDb()), identity),
    )

    class FakeEvolutionManager:
        @staticmethod
        async def after_reply(
            _factory: Any,
            *,
            identity: Identity,
            paths: Any,
            chat_id: str,
            session_id: str,
        ) -> None:
            # The fork Agent's own root span, as the TracingMiddleware names it.
            with tracer.start_as_current_span("invoke_agent skill_evolution"):
                pass

    monkeypatch.setitem(
        sys.modules,
        "qwenpaw_data.host.core.algo.skill_evolution",
        SimpleNamespace(SkillEvolutionManager=FakeEvolutionManager),
    )

    source_trace_id = trace_id_from_chat_id(chat_id)
    assert source_trace_id is not None
    token = bind_chat_trace(chat_id)
    try:
        with tracer.start_as_current_span("invoke_agent qwenpaw-data"):
            await mw._run()
    finally:
        reset_chat_trace(token)

    by_name = {span.name: span for span in exporter.get_finished_spans()}
    # The middleware's wrapper span is the detached trace root; it carries the
    # derived labels and surfaces after_reply failures as an ERROR span.
    root = by_name["skill_evolution"]
    assert root.parent is None
    assert format(root.context.trace_id, "032x") != source_trace_id
    assert root.attributes["langfuse.trace.name"] == "skill_evolution"
    assert list(root.attributes["langfuse.trace.tags"]) == ["skill_evolution"]
    assert root.attributes["session.id"] == "ses_1"
    assert root.attributes["user.id"] == "u1"
    metadata = json.loads(root.attributes["langfuse.trace.metadata"])
    assert metadata["source_trace_id"] == source_trace_id
    # The fork agent's own root span nests under the wrapper, same new trace.
    evolution = by_name["invoke_agent skill_evolution"]
    assert evolution.parent is not None
    assert evolution.parent.span_id == root.context.span_id
    assert evolution.context.trace_id == root.context.trace_id


def test_format_confirmed_settlement_cards() -> None:
    text = ConfirmedSettlementPromptMiddleware.format_cards(
        [
            {
                "type": "metric_caliber",
                "fields": {"caliber": "支付金额", "metric_name": "GMV"},
            }
        ]
    )
    assert text.startswith("<confirmed_settlement_cards>")
    assert text.endswith("</confirmed_settlement_cards>")
    assert "type=metric_caliber" in text
    assert '"metric_name": "GMV"' in text
    assert '"caliber": "支付金额"' in text


@pytest.mark.asyncio
async def test_confirmed_settlement_prompt_middleware() -> None:
    await reset_engine()
    await init_db()
    identity = Identity(
        user_id=TEST_USER_ID,
        tenant_id=TEST_TENANT_ID,
        workspace_id=TEST_WORKSPACE_ID,
    )
    factory = get_session_factory()
    async with factory() as db:
        session = Session.create(
            identity=identity,
            agent_id="default",
            title="settlement-prompt",
        )
        await SessionRepository(db, identity).add(session)
        repo = SettlementRepository(db, identity)
        pending = await repo.add(
            session_id=session.id,
            source_chat_id="chat_1",
            type="dimension_def",
            fields={"dimension_name": "渠道", "domain": "交易"},
        )
        confirmed = await repo.add(
            session_id=session.id,
            source_chat_id="chat_1",
            type="metric_caliber",
            fields={"metric_name": "GMV", "caliber": "支付金额"},
        )
        await repo.confirm(confirmed["id"], session_id=session.id)
        await db.commit()
        session_id = session.id
        _ = pending

    from qwenpaw_data.host.core.paths import UserAgentPaths

    run_context = RunContext(
        session_id=session_id,
        chat_id="chat_1",
        workspace=SimpleNamespace(),
        paths=UserAgentPaths(
            home=Path("."),
            tenant_id=TEST_TENANT_ID,
            workspace_id=TEST_WORKSPACE_ID,
            user_id=TEST_USER_ID,
            agent_id="a1",
            session_id=session_id,
        ),
        user_runtime_config=SimpleNamespace(),  # type: ignore[arg-type]
        identity=identity,
        session_factory=factory,
    )
    mw = ConfirmedSettlementPromptMiddleware(
        run_context=run_context,
        envelope=_FakeEnvelope(factory, identity),
    )

    with_confirmed = await mw.on_system_prompt(
        agent=SimpleNamespace(),
        current_prompt="BASE",
    )
    assert "<confirmed_settlement_cards>" in with_confirmed
    assert "metric_caliber" in with_confirmed
    assert "GMV" in with_confirmed
    assert "dimension_def" not in with_confirmed

    async with factory() as db:
        other = Session.create(
            identity=identity,
            agent_id="default",
            title="empty-prompt",
        )
        await SessionRepository(db, identity).add(other)
        await db.commit()
        empty_session_id = other.id

    empty_mw = ConfirmedSettlementPromptMiddleware(
        run_context=RunContext(
            session_id=empty_session_id,
            chat_id="chat_2",
            workspace=SimpleNamespace(),
            paths=UserAgentPaths(
                home=Path("."),
                tenant_id=TEST_TENANT_ID,
                workspace_id=TEST_WORKSPACE_ID,
                user_id=TEST_USER_ID,
                agent_id="a1",
                session_id=empty_session_id,
            ),
            user_runtime_config=SimpleNamespace(),  # type: ignore[arg-type]
            identity=identity,
            session_factory=factory,
        ),
        envelope=_FakeEnvelope(factory, identity),
    )
    assert (
        await empty_mw.on_system_prompt(
            agent=SimpleNamespace(),
            current_prompt="BASE",
        )
        == "BASE"
    )
